"""
Load sweep, in-process, virtual time (no network, no switches): exercises the real protocol
parser, admission control and path selection. Compares trying both paths vs only the shortest.

  python3 -m eval.sim [--n 1000] [--seeds 5] [--loads 0.2,0.4,...] [--out results/sim.csv]
"""
import argparse
import csv
import os
import statistics
import time

from controller import resmgr
from controller.service import ReservationService
from . import workload


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class NullInstaller:
    def install(self, r):
        pass

    def remove(self, r):
        pass


FIELDS = ["seed", "max_paths", "rho", "n", "accepted", "rej_capacity", "rej_conflict",
          "rej_malformed", "rej_other", "accept_ratio", "volume_ratio", "util_mean",
          "lat_us_mean", "lat_us_p95"]


def run_one(rho, seed, max_paths, n):
    topo = resmgr.diamond(reservable=0.8)
    mgr = resmgr.ResourceManager(topo, unique_flows=True, max_paths=max_paths)
    clk = Clock()
    svc = ReservationService(mgr, NullInstaller(), clk)
    reqs = workload.generate(n, workload.rate_for_load(rho), seed)

    lat, rej = [], {}
    accepted = 0
    for r in reqs:
        clk.t = r["t"]
        t0 = time.perf_counter()
        resp = svc.handle(r["line"])[0]
        lat.append((time.perf_counter() - t0) * 1e6)
        if resp.startswith("OK"):
            accepted += 1
        else:
            reason = resp.split()[1]
            rej[reason] = rej.get(reason, 0) + 1

    core = [l for l in topo.cap if l[0] in topo.switches and l[1] in topo.switches]
    t0, t1 = reqs[n // 5]["t"], reqs[-1]["t"]              # skip warm-up, stop at last arrival
    used = 0.0
    for r in mgr.res.values():
        ov = max(0.0, min(r.end, t1) - max(r.start, t0))
        used += sum(r.kbps * ov for l in r.links if l in core)
    util = used / (len(core) * topo.cap[core[0]] * (t1 - t0))
    offered = sum(q["kbps"] * q["dur"] for q in reqs if not q["bad"])
    got = sum(r.kbps * (r.end - r.start) for r in mgr.res.values())
    lat.sort()
    return dict(seed=seed, max_paths=max_paths or 2, rho=rho, n=n, accepted=accepted,
                rej_capacity=rej.get("CAPACITY", 0), rej_conflict=rej.get("FLOW_CONFLICT", 0),
                rej_malformed=rej.get("MALFORMED", 0),
                rej_other=sum(v for k, v in rej.items() if k not in ("CAPACITY", "FLOW_CONFLICT", "MALFORMED")),
                accept_ratio=accepted / n, volume_ratio=got / offered if offered else 0.0,
                util_mean=util, lat_us_mean=statistics.mean(lat), lat_us_p95=lat[int(0.95 * (len(lat) - 1))])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--loads", default="0.2,0.4,0.6,0.8,1.0,1.3,1.6,2.0")
    ap.add_argument("--out", default="results/sim.csv")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    loads = [float(x) for x in a.loads.split(",")]
    rows = []
    for mp in (1, None):
        for rho in loads:
            for seed in range(1, a.seeds + 1):
                rows.append(run_one(rho, seed, mp, a.n))
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        w.writerows(rows)

    print("%-9s %5s | %8s %8s | %6s %6s %6s | %8s %8s" %
          ("paths", "rho", "accept", "volume", "cap", "conf", "malf", "util", "p95 us"))
    for mp in (1, 2):
        for rho in loads:
            g = [r for r in rows if r["max_paths"] == mp and r["rho"] == rho]
            m = lambda k: statistics.mean(r[k] for r in g)
            print("%-9s %5.2f | %7.1f%% %7.1f%% | %6.0f %6.0f %6.0f | %7.1f%% %8.0f" %
                  ("1 (short)" if mp == 1 else "2 (all)", rho, 100 * m("accept_ratio"),
                   100 * m("volume_ratio"), m("rej_capacity"), m("rej_conflict"),
                   m("rej_malformed"), 100 * m("util_mean"), m("lat_us_p95")))
    print("\nwrote", a.out)


if __name__ == "__main__":
    main()
