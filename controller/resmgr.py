"""
Resource manager: topology, reservations, conflict detection, lifecycle.
Pure Python, no networking. All times are plain floats (seconds); pass `now`
explicitly in tests, default is time.time().

Model: capacity is tracked per DIRECTED link. A reservation reserves kbps on every
directed link of its src->dst path for the half-open interval [start, end).
"""
import threading
import time
from collections import Counter, namedtuple
from dataclasses import dataclass, field

PENDING, ACTIVE, EXPIRED, CANCELLED = "PENDING", "ACTIVE", "EXPIRED", "CANCELLED"
LIVE = (PENDING, ACTIVE)          # states that hold capacity


class Topology:
    def __init__(self):
        self.switches = {}   # name -> dpid
        self.hosts = {}      # ip -> host name
        self.adj = {}        # node -> [neighbours]
        self.cap = {}        # (u, v) directed -> kbps
        self.port = {}       # (u, v) -> port number on u facing v
        self.be_kbps = 0     # best-effort share per ingress switch (set by diamond())

    def add_switch(self, name, dpid):
        self.switches[name] = dpid
        self.adj.setdefault(name, [])

    def add_host(self, name, ip):
        self.hosts[ip] = name
        self.adj.setdefault(name, [])

    def add_link(self, u, v, kbps, port_u=0, port_v=0):
        self.adj[u].append(v)
        self.adj[v].append(u)
        self.cap[(u, v)] = self.cap[(v, u)] = kbps
        self.port[(u, v)] = port_u
        self.port[(v, u)] = port_v

    def paths(self, src, dst):
        """All simple paths src->dst (hosts never transit), shortest first."""
        out = []

        def dfs(node, path):
            if node == dst:
                out.append(list(path))
                return
            for nxt in self.adj[node]:
                if nxt in path:
                    continue
                if nxt != dst and nxt not in self.switches:
                    continue
                path.append(nxt)
                dfs(nxt, path)
                path.pop()

        dfs(src, [src])
        out.sort(key=lambda p: (len(p), p))
        return out

    @staticmethod
    def links(path):
        return list(zip(path, path[1:]))

    def switch_hops(self, path):
        """[(dpid, in_port, out_port)] for each switch on the path."""
        hops = []
        for i in range(1, len(path) - 1):
            sw = path[i]
            hops.append((self.switches[sw],
                         self.port[(sw, path[i - 1])],
                         self.port[(sw, path[i + 1])]))
        return hops


def diamond(core_kbps=10000, access_kbps=100000, reservable=1.0):
    """Must match topo/diamond.py (ports and core link rate)."""
    t = Topology()
    t.be_kbps = int(round(core_kbps * (1 - reservable)))   # left for best-effort traffic
    core_kbps = int(round(core_kbps * reservable))         # admission only sees the reservable share
    for i in (1, 2, 3, 4):
        t.add_switch("s%d" % i, i)
        t.add_host("h%d" % i, "10.0.0.%d" % i)
    t.add_link("h1", "s1", access_kbps, 0, 1)
    t.add_link("h3", "s1", access_kbps, 0, 2)
    t.add_link("h2", "s4", access_kbps, 0, 1)
    t.add_link("h4", "s4", access_kbps, 0, 2)
    t.add_link("s1", "s2", core_kbps, 3, 1)
    t.add_link("s1", "s3", core_kbps, 4, 1)
    t.add_link("s2", "s4", core_kbps, 2, 3)
    t.add_link("s3", "s4", core_kbps, 2, 4)
    return t


@dataclass
class Reservation:
    id: int
    src_ip: str
    dst_ip: str
    kbps: float
    start: float
    end: float
    path: list
    links: frozenset
    state: str = PENDING
    created: float = 0.0
    flow: tuple = None   # (proto, port) or None = whole host pair


Result = namedtuple("Result", "ok rid reason path")


class ResourceManager:
    def __init__(self, topo, unique_flows=False, max_paths=None):
        self.topo = topo
        self.max_paths = max_paths         # try only the k shortest paths (None = all); for evaluation
        self.unique_flows = unique_flows   # reject overlapping reservations with identical flow match
        self.lock = threading.RLock()     # public: server can hold it across manager + flow install
        self.res = {}                     # id -> Reservation
        self.next_id = 1
        self.counts = Counter()           # accepted / rejected / reason:<X>

    # ------------------------------------------------------------ admission
    def reserve(self, src_ip, dst_ip, kbps, start, end, now=None, flow=None):
        now = time.time() if now is None else now
        with self.lock:
            r = self._admit(src_ip, dst_ip, kbps, start, end, now, flow)
            if r.ok:
                self.counts["accepted"] += 1
            else:
                self.counts["rejected"] += 1
                self.counts["reason:" + r.reason] += 1
            return r

    def _admit(self, src_ip, dst_ip, kbps, start, end, now, flow=None):
        if not isinstance(kbps, (int, float)) or kbps <= 0 or end <= start or end <= now:
            return Result(False, None, "BAD_REQUEST", None)
        src = self.topo.hosts.get(src_ip)
        dst = self.topo.hosts.get(dst_ip)
        if src is None or dst is None:
            return Result(False, None, "UNKNOWN_HOST", None)
        if src == dst:
            return Result(False, None, "BAD_REQUEST", None)
        start = max(start, now)           # can't reserve the past
        if flow is not None and (flow[0] not in ("tcp", "udp") or not 1 <= flow[1] <= 65535):
            return Result(False, None, "BAD_REQUEST", None)
        # two live reservations with the same match would overwrite each other's flow rule
        if self.unique_flows and any(
                r.state in LIVE and r.src_ip == src_ip and r.dst_ip == dst_ip and r.flow == flow
                and r.start < end and r.end > start for r in self.res.values()):
            return Result(False, None, "FLOW_CONFLICT", None)
        paths = self.topo.paths(src, dst)
        if not paths:
            return Result(False, None, "NO_PATH", None)
        for p in (paths if self.max_paths is None else paths[:self.max_paths]):   # shortest feasible wins
            if self._fits(p, start, end, kbps):
                rid = self.next_id
                self.next_id += 1
                self.res[rid] = Reservation(rid, src_ip, dst_ip, kbps, start, end, p,
                                            frozenset(self.topo.links(p)), PENDING, now, flow)
                return Result(True, rid, "", p)
        return Result(False, None, "CAPACITY", None)

    def _fits(self, path, s, e, kbps):
        return all(self._peak(l, s, e) + kbps <= self.topo.cap[l] for l in self.topo.links(path))

    def _peak(self, link, s, e):
        """Max load already reserved on `link` at any instant in [s, e)."""
        live = [r for r in self.res.values()
                if r.state in LIVE and link in r.links and r.start < e and r.end > s]
        pts = [s] + [r.start for r in live if r.start > s]   # load only rises at a start
        return max((sum(r.kbps for r in live if r.start <= p < r.end) for p in pts), default=0)

    # ------------------------------------------------------------ lifecycle
    def cancel(self, rid):
        with self.lock:
            r = self.res.get(rid)
            if r is None or r.state not in LIVE:
                return None
            r.state = CANCELLED
            return r

    def tick(self, now=None):
        """Advance states. Returns (activated, expired) lists of Reservation."""
        now = time.time() if now is None else now
        activated, expired = [], []
        with self.lock:
            for r in self.res.values():
                if r.state in LIVE and r.end <= now:
                    r.state = EXPIRED
                    expired.append(r)
                elif r.state == PENDING and r.start <= now:
                    r.state = ACTIVE
                    activated.append(r)
        return activated, expired

    # ------------------------------------------------------------ queries
    def get(self, rid):
        with self.lock:
            return self.res.get(rid)

    def list(self, states=None):
        with self.lock:
            return [r for r in self.res.values() if states is None or r.state in states]

    def utilization(self, t=None):
        """{(u, v): (reserved_kbps, capacity_kbps)} at time t."""
        t = time.time() if t is None else t
        with self.lock:
            used = Counter()
            for r in self.res.values():
                if r.state in LIVE and r.start <= t < r.end:
                    for l in r.links:
                        used[l] += r.kbps
            return {l: (used[l], c) for l, c in self.topo.cap.items()}
