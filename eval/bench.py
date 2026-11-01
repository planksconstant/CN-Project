"""
Micro-benchmarks behind the scalability claims (no switches involved).
  python3 -m eval.bench
"""
import socket
import statistics
import threading
import time

from controller.client import request
from controller.resmgr import ResourceManager, Topology, diamond
from controller.server import LineReader, ReservationServer
from controller.service import ReservationService


def leaf_spine(leaves, spines):
    t = Topology()
    for s in range(spines):
        t.add_switch("sp%d" % s, 100 + s)
    for l in range(leaves):
        t.add_switch("lf%d" % l, l + 1)
        t.add_host("h%d" % l, "10.1.%d.%d" % (l // 200, l % 200 + 1))
        t.add_link("h%d" % l, "lf%d" % l, 100000, 0, 1)
        for s in range(spines):
            t.add_link("lf%d" % l, "sp%d" % s, 8000, 2 + s, l + 1)
    return t


def one_admission(m, i):
    t0 = time.perf_counter()
    m.reserve("10.0.0.3", "10.0.0.4", 1, 1e9 + i, 1e9 + i + 1, now=0, flow=("udp", 7 + i))
    return (time.perf_counter() - t0) * 1000


def bench_history():
    print("1) admission cost vs number of reservations held (ms per request, median of 20)")
    print("   %-26s %10s %10s %10s" % ("", "1,000", "10,000", "50,000"))
    row = []
    for n in (1000, 10000, 50000):                       # FINISHED reservations: history only
        m = ResourceManager(diamond(reservable=0.8), unique_flows=True)
        for i in range(n):
            now = i * 2 + 1.5
            m.tick(now)
            m.reserve("10.0.0.1", "10.0.0.2", 1, now, now + 1, now=now, flow=("udp", 1 + i % 60000))
        m.tick(n * 2 + 10)
        row.append(statistics.median(one_admission(m, i) for i in range(20)))
    print("   %-26s %10.3f %10.3f %10.3f" % ("finished (history)", *row))
    row = []
    for n in (1000, 5000, 10000):                        # LIVE reservations on the same link
        m = ResourceManager(diamond(reservable=0.8), unique_flows=True)
        for i in range(n):
            m.reserve("10.0.0.1", "10.0.0.2", 1, 100 + i * 2, 101 + i * 2, now=0, flow=("udp", 1 + i % 60000))
        row.append(statistics.median(one_admission(m, i) for i in range(20)))
    print("   %-26s %10.3f %10.3f %10.3f   (columns: 1,000 / 5,000 / 10,000 live)" % ("live on the link", *row))


def bench_paths():
    print("\n2) path search on leaf-spine topologies (cold, ms): all simple paths vs bounded (shortest+2)")
    print("   %-14s %10s %12s %12s" % ("leaves x spines", "paths(all)", "all (ms)", "bounded (ms)"))
    for leaves, spines in ((4, 2), (6, 3), (8, 4)):
        t = leaf_spine(leaves, spines)
        t0 = time.perf_counter()
        allp = t.paths("h0", "h%d" % (leaves - 1), slack=None)
        t_all = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        bp = t.paths("h0", "h%d" % (leaves - 1), slack=2)
        t_b = (time.perf_counter() - t0) * 1000
        print("   %-14s %10d %12.2f %12.2f   (bounded keeps %d)" % ("%dx%d" % (leaves, spines), len(allp), t_all, t_b, len(bp)))
    t = leaf_spine(64, 8)
    for slack in (0, 2):
        t0 = time.perf_counter()
        n = len(t.paths("h0", "h63", slack=slack))
        print("   64x8, slack=%d: %d paths in %.2f ms cold" % (slack, n, (time.perf_counter() - t0) * 1000))
    t0 = time.perf_counter()
    t.paths("h0", "h63", slack=2)
    print("   64x8, repeat call (memo): %.4f ms" % ((time.perf_counter() - t0) * 1000))


class Null:
    def install(self, r):
        pass

    def remove(self, r):
        pass


def bench_throughput():
    print("\n3) end-to-end RESERVE throughput over TCP (stub installer, loopback, Python threads)")
    print("   %-8s %12s %12s %10s" % ("clients", "requests/s", "p95 ms", "accepted"))
    for clients in (1, 4, 16, 64):
        mgr = ResourceManager(diamond(reservable=0.8), unique_flows=True)
        srv = ReservationServer(ReservationService(mgr, Null()), "127.0.0.1", 0, max_clients=512)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        per = 2000 // clients
        lat, acc, lock = [], [0], threading.Lock()

        def worker(w):
            s = socket.create_connection(("127.0.0.1", srv.port))
            rd = LineReader(s)
            mine, ok = [], 0
            for i in range(per):
                a, b = (("10.0.0.1", "10.0.0.2"), ("10.0.0.3", "10.0.0.4"), ("10.0.0.2", "10.0.0.1"), ("10.0.0.4", "10.0.0.3"))[(w + i) % 4]
                t0 = time.perf_counter()
                r = request(s, rd, "RESERVE %s %s 10 0 600 udp/%d" % (a, b, 1 + w * per + i))[0]
                mine.append((time.perf_counter() - t0) * 1000)
                ok += r.startswith("OK")
            s.close()
            with lock:
                lat.extend(mine)
                acc[0] += ok
        ts = [threading.Thread(target=worker, args=(w,)) for w in range(clients)]
        t0 = time.perf_counter()
        [t.start() for t in ts]
        [t.join() for t in ts]
        dt = time.perf_counter() - t0
        lat.sort()
        print("   %-8d %12.0f %12.2f %10d" % (clients, per * clients / dt, lat[int(0.95 * (len(lat) - 1))], acc[0]))
        srv.close()


if __name__ == "__main__":
    bench_history()
    bench_paths()
    bench_throughput()
