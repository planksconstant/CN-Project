"""
Reservation service: one text command line in -> list of response lines out.
Owns the glue between ResourceManager (state) and an installer (switch policy).

Locking: mgr.lock is held while the installer runs, so reservation state and
installed flows can't drift apart. Lock order is always mgr.lock -> switch send lock.
"""
import logging
import math
import threading
import time

log = logging.getLogger("svc")
END = "END"


class LogInstaller:
    """Stub installer: logs what would be pushed to switches. Step 7 replaces it."""

    def __init__(self, topo):
        self.topo = topo
        self.installed, self.removed = [], []

    def install(self, r):
        self.installed.append(r.id)
        log.info("INSTALL rid=%d kbps=%g hops=%s", r.id, r.kbps, self.topo.switch_hops(r.path))

    def remove(self, r):
        self.removed.append(r.id)
        log.info("REMOVE rid=%d", r.id)


class ReservationService:
    def __init__(self, mgr, installer, clock=time.time):
        self.mgr = mgr
        self.installer = installer
        self.clock = clock
        self._installed = set()           # rids whose policy is currently on the switches

    # ------------------------------------------------------------ lifecycle sync
    def sync(self):
        """Advance reservation states; install newly active, remove expired."""
        with self.mgr.lock:
            activated, expired = self.mgr.tick(self.clock())
            for r in expired:
                if r.id in self._installed:
                    self._remove(r)
            for r in activated:
                try:
                    self.installer.install(r)
                    self._installed.add(r.id)
                except Exception as e:
                    log.error("install failed rid=%d (%s): cancelling", r.id, e)
                    self.mgr.cancel(r.id)
                    self.mgr.counts["install_failed"] += 1

    def _remove(self, r):
        try:
            self.installer.remove(r)
        except Exception:
            log.exception("remove failed rid=%d", r.id)
        self._installed.discard(r.id)

    def run_ticker(self, interval=0.5):
        def loop():
            while True:
                time.sleep(interval)
                try:
                    self.sync()
                except Exception:
                    log.exception("sync failed")
        t = threading.Thread(target=loop, daemon=True, name="ticker")
        t.start()
        return t

    # ------------------------------------------------------------ protocol
    def handle(self, line):
        parts = line.split()
        if not parts:
            return ["ERR EMPTY"]
        fn = getattr(self, "_cmd_" + parts[0].lower(), None)
        if fn is None:
            return ["ERR BAD_COMMAND"]
        self.sync()
        return fn(parts[1:])

    def _malformed(self):
        with self.mgr.lock:
            self.mgr.counts["rejected"] += 1
            self.mgr.counts["reason:MALFORMED"] += 1
        return ["REJECT MALFORMED"]

    def _cmd_reserve(self, a):
        # RESERVE <src_ip> <dst_ip> <kbps> <start_in_sec> <duration_sec>
        # optional 6th arg: <tcp|udp>/<dst_port> narrows the reservation to one flow
        if len(a) not in (5, 6):
            return self._malformed()
        try:
            kbps, start_in, dur = float(a[2]), float(a[3]), float(a[4])
            flow = None
            if len(a) == 6:
                proto, port = a[5].lower().split("/")
                flow = (proto, int(port))
        except ValueError:
            return self._malformed()
        if not all(math.isfinite(x) for x in (kbps, start_in, dur)):
            return self._malformed()
        with self.mgr.lock:
            now = self.clock()
            res = self.mgr.reserve(a[0], a[1], kbps, now + start_in, now + start_in + dur,
                                   now=now, flow=flow)
            if not res.ok:
                return ["REJECT " + res.reason]
            self.sync()                    # start_in == 0 -> activate right away
            if self.mgr.get(res.rid).state == "CANCELLED":     # installer refused it
                c = self.mgr.counts
                c["accepted"] -= 1
                c["rejected"] += 1
                c["reason:INSTALL_FAILED"] += 1
                return ["REJECT INSTALL_FAILED"]
            return ["OK %d %s" % (res.rid, "-".join(res.path))]

    def _cmd_cancel(self, a):
        if len(a) != 1 or not a[0].isdigit():
            return ["ERR BAD_ARGS"]
        rid = int(a[0])
        with self.mgr.lock:
            if self.mgr.get(rid) is None:
                return ["ERR NOT_FOUND"]
            was_installed = rid in self._installed
            r = self.mgr.cancel(rid)
            if r is None:
                return ["ERR NOT_CANCELLABLE"]
            if was_installed:
                self._remove(r)
            return ["OK"]

    def _fmt(self, r, now):
        return "%d %s %s %s %g %.1f %.1f %s %s" % (
            r.id, r.state, r.src_ip, r.dst_ip, r.kbps,
            r.start - now, r.end - now, "-".join(r.path),
            "%s/%d" % r.flow if r.flow else "-")

    def _cmd_status(self, a):
        # -> OK <id> <state> <src> <dst> <kbps> <start_in> <end_in> <path>
        if len(a) != 1 or not a[0].isdigit():
            return ["ERR BAD_ARGS"]
        r = self.mgr.get(int(a[0]))
        if r is None:
            return ["ERR NOT_FOUND"]
        return ["OK " + self._fmt(r, self.clock())]

    def _cmd_list(self, a):
        # LIST [ALL]  -> OK <n>, n lines, END   (default: PENDING + ACTIVE only)
        from .resmgr import LIVE
        rs = self.mgr.list(None if a and a[0].upper() == "ALL" else LIVE)
        now = self.clock()
        return ["OK %d" % len(rs)] + [self._fmt(r, now) for r in rs] + [END]

    def _cmd_util(self, a):
        # core (switch-switch) directed links: <u>-<v> <reserved_kbps> <cap_kbps> <pct>
        topo = self.mgr.topo
        u = self.mgr.utilization(self.clock())
        lines = ["%s-%s %g %g %.1f" % (x, y, used, cap, 100.0 * used / cap)
                 for (x, y), (used, cap) in sorted(u.items())
                 if x in topo.switches and y in topo.switches]
        return ["OK %d" % len(lines)] + lines + [END]

    def _cmd_stats(self, a):
        with self.mgr.lock:
            c = self.mgr.counts
            items = ["accepted=%d" % c["accepted"], "rejected=%d" % c["rejected"]]
            items += ["%s=%d" % (k, v) for k, v in sorted(c.items()) if k.startswith("reason:")]
        return ["OK " + " ".join(items)]

    def _cmd_quit(self, a):
        return ["OK BYE"]
