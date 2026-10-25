"""
Step 8: poll port + meter counters every `interval` s and log to CSV.
  logs/util.csv    ts,link,reserved_kbps,cap_kbps,measured_kbps   (directed core links)
  logs/meters.csv  ts,dpid,meter,bytes_in,bytes_dropped           (meter = rid, or 900000 = best effort)
Requests go out from the poller thread; replies arrive on each switch's reader thread (on_reply).
"""
import csv
import logging
import os
import threading
import time

from . import of13

log = logging.getLogger("stats")


class StatsPoller:
    def __init__(self, topo, mgr, get_switches, clock=time.time, outdir="logs", interval=2.0):
        self.topo, self.mgr, self.get_switches = topo, mgr, get_switches
        self.clock, self.interval = clock, interval
        self.ports = {}        # (dpid, port) -> (t, tx_bytes)
        self.meters = {}       # (dpid, meter_id) -> (t, bytes_in, bytes_dropped)
        self._prev = {}
        self._lock = threading.Lock()
        os.makedirs(outdir, exist_ok=True)
        self._uf = open(os.path.join(outdir, "util.csv"), "w", newline="")
        self._mf = open(os.path.join(outdir, "meters.csv"), "w", newline="")
        self._uw, self._mw = csv.writer(self._uf), csv.writer(self._mf)
        self._uw.writerow(["ts", "link", "reserved_kbps", "cap_kbps", "measured_kbps"])
        self._mw.writerow(["ts", "dpid", "meter", "bytes_in", "bytes_dropped"])

    # ------------------------------------------------------------ replies (reader threads)
    def on_reply(self, sw, body):
        try:
            mp_type, _flags, data = of13.parse_multipart_reply(body)
            now = self.clock()
            if mp_type == of13.OFPMP_PORT_STATS:
                with self._lock:
                    for p in of13.parse_port_stats(data):
                        self.ports[(sw.dpid, p["port"])] = (now, p["tx_bytes"])
            elif mp_type == of13.OFPMP_METER:
                with self._lock:
                    for m in of13.parse_meter_stats(data):
                        dropped = sum(b[1] for b in m["bands"])
                        self.meters[(sw.dpid, m["meter_id"])] = (now, m["byte_in"], dropped)
        except Exception:
            log.exception("bad stats reply from %s", sw)

    # ------------------------------------------------------------ polling
    def poll_once(self):
        for sw in self.get_switches():
            try:
                sw.send(of13.port_stats_request(xid=sw.xid()))
                sw.send(of13.meter_stats_request(xid=sw.xid()))
            except OSError:
                pass

    def log_cycle(self, now=None):
        now = self.clock() if now is None else now
        with self._lock:
            cur, prev = dict(self.ports), self._prev
            self._prev = cur
            meters = dict(self.meters)
        for (u, v), (reserved, cap) in sorted(self.mgr.utilization(now).items()):
            if u not in self.topo.switches or v not in self.topo.switches:
                continue
            key = (self.topo.switches[u], self.topo.port[(u, v)])
            c, p = cur.get(key), prev.get(key)
            measured = ""
            if c and p and c[0] > p[0]:
                measured = "%.1f" % ((c[1] - p[1]) * 8 / 1000.0 / (c[0] - p[0]))
            self._uw.writerow(["%.3f" % now, "%s-%s" % (u, v), "%g" % reserved, "%g" % cap, measured])
        for (dpid, mid), (_t, bin_, dropped) in sorted(meters.items()):
            self._mw.writerow(["%.3f" % now, dpid, mid, bin_, dropped])
        self._uf.flush()
        self._mf.flush()

    def start(self):
        def loop():
            while True:
                try:
                    self.poll_once()
                    time.sleep(self.interval)
                    self.log_cycle()
                except Exception:
                    log.exception("poll cycle failed")
        t = threading.Thread(target=loop, daemon=True, name="stats")
        t.start()
        return t
