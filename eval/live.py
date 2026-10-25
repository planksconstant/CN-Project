"""
Real-time load against the running controller, over TCP, from concurrent clients.
Measures end-to-end admission latency (request sent -> reply received). With Mininet up the
latency includes installing flows on the switches; with --stub-installer it is admission only.

  python3 -m eval.live --rho 0.8 --n 60            # Poisson arrivals, short reservations
  python3 -m eval.live --burst 40                  # 40 clients fire at the same instant
"""
import argparse
import csv
import os
import socket
import statistics
import sys
import threading
import time

from controller.client import request
from controller.server import LineReader
from . import workload


def one(host, port, line, start_at, out, i, gate=None):
    try:
        s = socket.create_connection((host, port))
        rd = LineReader(s)
        if gate:
            gate.wait()
        else:
            time.sleep(max(0.0, start_at - time.time()))
        t0 = time.perf_counter()
        resp = request(s, rd, line)[0]
        out[i] = (resp, (time.perf_counter() - t0) * 1000)
        s.close()
    except Exception:                       # connection refused/reset, broken barrier, ...
        out[i] = ("FAILED", 0.0)
        if gate:
            gate.abort()                    # don't leave the other burst clients waiting forever


def ask(host, port, line):
    s = socket.create_connection((host, port))
    try:
        return request(s, LineReader(s), line)
    finally:
        s.close()


def summarize(results):
    failed = sum(1 for r in results if r[0] == "FAILED")
    results = [r for r in results if r[0] != "FAILED"]
    if failed:
        print("WARNING: %d requests failed (connection or protocol errors)" % failed)
    if not results:
        sys.exit("no request completed")
    ok = [r for r in results if r[0].startswith("OK")]
    reasons = {}
    for resp, _ in results:
        if resp.startswith("REJECT"):
            reasons[resp.split()[1]] = reasons.get(resp.split()[1], 0) + 1
    lat = sorted(l for _, l in results)
    pct = lambda p: lat[int(p * (len(lat) - 1))]
    print("requests %d  accepted %d (%.1f%%)  rejected %d  %s" %
          (len(results), len(ok), 100.0 * len(ok) / len(results), len(results) - len(ok), reasons))
    print("latency ms: mean %.2f  p50 %.2f  p95 %.2f  max %.2f" %
          (statistics.mean(lat), pct(0.5), pct(0.95), lat[-1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--rho", type=float, default=0.8, help="offered load, see workload.py")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--dur", default="4,10", help="reservation length range, seconds")
    ap.add_argument("--burst", type=int, default=0, help="N simultaneous clients instead of Poisson arrivals")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="results/live.csv")
    a = ap.parse_args()
    dur = tuple(float(x) for x in a.dur.split(","))
    try:
        ask(a.host, a.port, "STATS")
    except OSError:
        sys.exit("controller not reachable on %s:%d - start it first: python3 -m controller.main"
                 % (a.host, a.port))

    if a.burst:
        reqs = workload.generate(a.burst, 1000.0, a.seed, dur=(25, 30), advance_frac=0, bad_frac=0)
        for r in reqs:
            r["t"] = 0.0
    else:
        reqs = workload.generate(a.n, workload.rate_for_load(a.rho, dur), a.seed, dur=dur)

    out = [None] * len(reqs)
    gate = threading.Barrier(len(reqs)) if a.burst else None
    t_start = time.time() + 0.5
    ts = [threading.Thread(target=one, args=(a.host, a.port, r["line"], t_start + r["t"], out, i, gate))
          for i, r in enumerate(reqs)]
    [t.start() for t in ts]
    [t.join() for t in ts]

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["i", "t", "line", "response", "latency_ms"])
        for i, (r, (resp, ms)) in enumerate(zip(reqs, out)):
            w.writerow([i, "%.3f" % r["t"], r["line"], resp, "%.3f" % ms])
    summarize(out)
    print("controller:", ask(a.host, a.port, "STATS")[0])
    if a.burst:
        util = ask(a.host, a.port, "UTIL")[1:]
        worst = max(float(l.split()[3]) for l in util)
        print("worst core-link utilization right after burst: %.1f%%  -> %s" %
              (worst, "OK, never oversubscribed" if worst <= 100.0 else "OVERSUBSCRIBED!"))
    print("wrote", a.out)


if __name__ == "__main__":
    main()
