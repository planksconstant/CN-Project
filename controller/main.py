#!/usr/bin/env python3
"""Run from repo root:  python3 -m controller.main [--debug]"""
import argparse
import logging
import socket
import threading

from . import of13, static_flows
from .switch import Switch

log = logging.getLogger("ctl")
ERR_NAMES = {0: "HELLO_FAILED", 1: "BAD_REQUEST", 2: "BAD_ACTION", 3: "BAD_INSTRUCTION",
             4: "BAD_MATCH", 5: "FLOW_MOD_FAILED", 6: "GROUP_MOD_FAILED", 7: "PORT_MOD_FAILED",
             8: "TABLE_MOD_FAILED", 9: "QUEUE_OP_FAILED", 10: "SWITCH_CONFIG_FAILED",
             11: "ROLE_REQUEST_FAILED", 12: "METER_MOD_FAILED"}

switches = {}                 # dpid -> Switch
switches_lock = threading.Lock()


def on_switch_ready(sw):
    log.info("switch up: dpid=%d", sw.dpid)
    static_flows.install(sw)


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
            f = of13.parse_features_reply(body)
            sw.dpid = f["dpid"]
            sw.ready = True
            with switches_lock:
                switches[sw.dpid] = sw
            log.debug("features: %s", f)
            on_switch_ready(sw)

        elif mtype == of13.OFPT_PACKET_IN:
            pi = of13.parse_packet_in(body)
            eth = of13.peek_eth(pi["data"])
            if eth and eth[2] == 0x86DD:      # drop IPv6 noise from mininet hosts
                continue
            log.info("PACKET_IN dpid=%s in_port=%s eth=%s", sw.dpid, pi["in_port"], eth)

        elif mtype == of13.OFPT_ERROR:
            et, code, data = of13.parse_error(body)
            log.error("ERROR from dpid=%s type=%s(%d) code=%d xid=%d",
                      sw.dpid, ERR_NAMES.get(et, "?"), et, code, xid)

        elif mtype == of13.OFPT_BARRIER_REPLY:
            log.debug("barrier reply xid=%d", xid)

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
                log.info("switch down: %s", sw)
                sw.close()

        threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=6653)
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        serve(a.port)
    except KeyboardInterrupt:
        pass
