#!/usr/bin/env python3
"""Run from repo root:  python3 -m controller.main [--debug] [--stub-installer]"""
import argparse
import logging
import socket
import threading
import time

from . import of13, resmgr, service, static_flows
from .installer import OFInstaller
from .server import ReservationServer
from .stats import StatsPoller
from .switch import Switch

log = logging.getLogger("ctl")
ERR_NAMES = {0: "HELLO_FAILED", 1: "BAD_REQUEST", 2: "BAD_ACTION", 3: "BAD_INSTRUCTION",
             4: "BAD_MATCH", 5: "FLOW_MOD_FAILED", 6: "GROUP_MOD_FAILED", 7: "PORT_MOD_FAILED",
             8: "TABLE_MOD_FAILED", 9: "QUEUE_OP_FAILED", 10: "SWITCH_CONFIG_FAILED",
             11: "ROLE_REQUEST_FAILED", 12: "METER_MOD_FAILED"}

switches = {}                 # dpid -> Switch
switches_lock = threading.Lock()
seen_dpids = set()            # wipe a switch's tables only the first time we see it
topo = None
stats = None


def get_switch(dpid):
    with switches_lock:
        return switches.get(dpid)


def ready_switches():
    with switches_lock:
        return [s for s in switches.values() if s.ready]


def on_switch_ready(sw):
    first = sw.dpid not in seen_dpids
    seen_dpids.add(sw.dpid)
    log.info("switch up: dpid=%d%s", sw.dpid, "" if first else " (reconnect)")
    static_flows.install(sw, topo, wipe=first)


def handle(sw):
    sw.send(of13.hello(sw.xid()))
    while True:
        msg = sw.recv()
        if msg is None:
            break
        ver, mtype, xid, body = msg
        if ver != of13.OFP_VERSION and mtype != of13.OFPT_HELLO:
            log.warning("unexpected version 0x%02x", ver)

        if mtype == of13.OFPT_HELLO:
            sw.send(of13.features_request(sw.xid()))

        elif mtype == of13.OFPT_ECHO_REQUEST:
            sw.send(of13.echo_reply(body, xid))

        elif mtype == of13.OFPT_FEATURES_REPLY:
            sw.dpid = of13.parse_features_reply(body)["dpid"]
            with switches_lock:
                switches[sw.dpid] = sw
            on_switch_ready(sw)
            sw.ready = True                       # reservations may use it from now on

        elif mtype == of13.OFPT_PACKET_IN:
            pi = of13.parse_packet_in(body)
            eth = of13.peek_eth(pi["data"])
            if eth and eth[2] == 0x86DD:          # drop IPv6 noise from mininet hosts
                continue
            log.info("PACKET_IN dpid=%s in_port=%s eth=%s", sw.dpid, pi["in_port"], eth)

        elif mtype == of13.OFPT_MULTIPART_REPLY:
            if stats:
                stats.on_reply(sw, body)

        elif mtype == of13.OFPT_BARRIER_REPLY:
            sw.on_barrier_reply(xid)

        elif mtype == of13.OFPT_ERROR:
            et, code, _data = of13.parse_error(body)
            sw.errors.append((xid, et, code))
            log.error("ERROR from dpid=%s type=%s(%d) code=%d xid=%d",
                      sw.dpid, ERR_NAMES.get(et, "?"), et, code, xid)

        else:
            log.debug("unhandled type=%d len=%d", mtype, len(body))


def serve(port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(16)
    log.info("OpenFlow listener on :%d", port)
    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sw = Switch(conn, addr)

        def run(sw=sw):
            try:
                handle(sw)
            except OSError as e:
                log.warning("conn error %s: %s", sw, e)
            finally:
                with switches_lock:
                    if sw.dpid is not None and switches.get(sw.dpid) is sw:
                        del switches[sw.dpid]
                sw.ready = False
                log.info("switch down: %s", sw)
                sw.close()

        threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=6653, help="OpenFlow listen port")
    ap.add_argument("--res-host", default="127.0.0.1")
    ap.add_argument("--res-port", type=int, default=9000, help="reservation TCP port")
    ap.add_argument("--reservable", type=float, default=0.8,
                    help="fraction of each core link admission control may hand out")
    ap.add_argument("--poll", type=float, default=2.0, help="stats polling interval (s)")
    ap.add_argument("--logdir", default="logs")
    ap.add_argument("--stub-installer", action="store_true", help="log only, don't touch switches")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    topo = resmgr.diamond(reservable=a.reservable)
    mgr = resmgr.ResourceManager(topo, unique_flows=True)
    installer = (service.LogInstaller(topo) if a.stub_installer
                 else OFInstaller(topo, get_switch, time.time))
    svc = service.ReservationService(mgr, installer)
    svc.run_ticker()
    stats = StatsPoller(topo, mgr, ready_switches, outdir=a.logdir, interval=a.poll)
    stats.start()
    rs = ReservationServer(svc, a.res_host, a.res_port)
    threading.Thread(target=rs.serve_forever, daemon=True, name="reservations").start()
    log.info("reservation server on %s:%d  (reservable=%.0f%%, best-effort cap=%d kbps)",
             a.res_host, rs.port, a.reservable * 100, topo.be_kbps)
    try:
        serve(a.port)
    except KeyboardInterrupt:
        pass
