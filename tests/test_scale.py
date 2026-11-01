import random
import socket
import threading
import unittest

from controller import resmgr
from controller.installer import OFInstaller
from controller.resmgr import ResourceManager, Topology, diamond
from controller.server import LineReader, ReservationServer
from controller.service import ReservationService
from tests.test_policy import Clock, FakeSwitch

H = ["10.0.0.%d" % i for i in (1, 2, 3, 4)]


def leaf_spine(leaves, spines):
    t = Topology()
    for s in range(spines):
        t.add_switch("sp%d" % s, 100 + s)
    for l in range(leaves):
        t.add_switch("lf%d" % l, l + 1)
        t.add_host("h%d" % l, "10.1.0.%d" % (l + 1))
        t.add_link("h%d" % l, "lf%d" % l, 100000, 0, 1)
        for s in range(spines):
            t.add_link("lf%d" % l, "sp%d" % s, 8000, 2 + s, l + 1)
    return t


class TestIndexes(unittest.TestCase):
    def brute_peak(self, m, link, s, e):
        live = [r for r in m.res.values() if r.state in resmgr.LIVE and link in r.links
                and r.start < e and r.end > s]
        pts = [s] + [r.start for r in live if r.start > s]
        return max((sum(r.kbps for r in live if r.start <= p < r.end) for p in pts), default=0)

    def test_indexed_peak_matches_brute_force_under_random_ops(self):
        rnd = random.Random(7)
        m = ResourceManager(diamond(reservable=0.8), unique_flows=True)
        now, ids = 0.0, []
        for step in range(600):
            now += rnd.uniform(0, 2)
            op = rnd.random()
            if op < 0.6:
                a, b = rnd.sample(H, 2)
                st = now + rnd.choice([0, 0, rnd.uniform(0, 20)])
                r = m.reserve(a, b, rnd.choice([500, 1000, 3000]), st, st + rnd.uniform(1, 15),
                              now=now, flow=("udp", rnd.randint(1, 30)))
                if r.ok:
                    ids.append(r.rid)
            elif op < 0.75 and ids:
                m.cancel(rnd.choice(ids))
            else:
                m.tick(now)
            if step % 25 == 0:
                for link in m.topo.cap:
                    for s, e in ((now, now + 5), (now + 3, now + 30)):
                        self.assertEqual(m._peak(link, s, e), self.brute_peak(m, link, s, e))
                self.assertEqual(set(m._live), {r.id for r in m.res.values() if r.state in resmgr.LIVE})

    def test_finished_reservations_leave_no_index_entries(self):
        m = ResourceManager(diamond(reservable=0.8), unique_flows=True)
        for i in range(50):
            m.reserve(H[0], H[1], 100, 0, 5 + i, now=0, flow=("udp", i + 1))
        m.cancel(3)
        m.tick(1000)
        self.assertEqual(len(m._live), 0)
        self.assertTrue(all(not d for d in m._by_link.values()))
        self.assertEqual(len(m._by_flow), 0)
        self.assertEqual(len(m.res), 50)                 # history is kept for STATUS / evaluation
        self.assertTrue(m.reserve(H[0], H[1], 8000, 1001, 1010, now=1000).ok)


class TestPaths(unittest.TestCase):
    def test_bounded_search_and_memo(self):
        t = leaf_spine(4, 2)
        shortest = t.paths("h0", "h3", slack=0)
        self.assertEqual(len(shortest), 2)
        self.assertTrue(all(len(p) == 5 for p in shortest))
        bounded, allp = t.paths("h0", "h3", slack=2), t.paths("h0", "h3", slack=None)
        self.assertLess(len(shortest), len(bounded))
        self.assertLessEqual(len(bounded), len(allp))
        self.assertTrue(all(p in allp for p in bounded))
        self.assertIs(t.paths("h0", "h3", slack=2), bounded)            # memoized
        t.add_link("lf0", "lf3", 8000, 9, 9)                            # topology change drops the memo
        self.assertEqual(len(t.paths("h0", "h3", slack=0)), 1)

    def test_disconnected_and_default(self):
        t = Topology()
        t.add_switch("s1", 1)
        t.add_host("h1", "10.0.0.1")
        t.add_host("h2", "10.0.0.2")
        t.add_link("h1", "s1", 1000, 0, 1)
        self.assertEqual(t.paths("h1", "h2"), [])
        self.assertEqual(len(diamond().paths("h1", "h2")), 2)           # diamond result unchanged

    def test_admission_works_on_a_bigger_topology(self):
        t = leaf_spine(6, 3)
        m = ResourceManager(t, unique_flows=True)
        ok = sum(m.reserve("10.1.0.%d" % (1 + i % 3), "10.1.0.%d" % (4 + i % 3), 2000, 0, 10,
                           now=0, flow=("udp", i + 1)).ok for i in range(30))
        self.assertGreater(ok, 6)
        for l, (used, cap) in m.utilization(5).items():
            self.assertLessEqual(used, cap)


class TestCounts(unittest.TestCase):
    def test_deferred_install_failure_is_counted_as_rejected(self):
        topo = diamond(reservable=0.8)
        mgr, clk = ResourceManager(topo, unique_flows=True), Clock()
        sws = {d: FakeSwitch(d) for d in (1, 2, 3, 4)}
        svc = ReservationService(mgr, OFInstaller(topo, sws.get, clk), clk)
        self.assertTrue(svc.handle("RESERVE 10.0.0.1 10.0.0.2 1000 10 30 udp/5201")[0].startswith("OK"))
        self.assertEqual(svc.handle("STATS"), ["OK accepted=1 rejected=0"])
        sws.pop(2)
        clk.t += 10
        svc.sync()
        self.assertEqual(svc.handle("STATUS 1")[0].split()[2], "CANCELLED")
        self.assertEqual(svc.handle("STATS"), ["OK accepted=0 rejected=1 reason:INSTALL_FAILED=1"])


class TestBarriers(unittest.TestCase):
    def test_all_barriers_sent_before_any_wait(self):
        trace = []
        topo = diamond(reservable=0.8)
        mgr, clk = ResourceManager(topo, unique_flows=True), Clock()
        sws = {d: FakeSwitch(d, trace) for d in (1, 2, 3, 4)}
        svc = ReservationService(mgr, OFInstaller(topo, sws.get, clk), clk)
        self.assertTrue(svc.handle("RESERVE 10.0.0.1 10.0.0.2 1000 0 30 udp/5201")[0].startswith("OK"))
        kinds = [k for k, _ in trace]
        self.assertEqual(kinds, ["start"] * 3 + ["wait"] * 3)


class TestServerCap(unittest.TestCase):
    def test_busy_when_full_and_slot_freed_on_close(self):
        class Null:
            install = remove = lambda self, r: None
        mgr = ResourceManager(diamond(reservable=0.8), unique_flows=True)
        srv = ReservationServer(ReservationService(mgr, Null()), "127.0.0.1", 0, max_clients=2)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def connect():
            s = socket.create_connection(("127.0.0.1", srv.port))
            s.settimeout(3)
            return s, LineReader(s)
        a, ra = connect()
        b, rb = connect()
        for s, r in ((a, ra), (b, rb)):                    # make sure both are really being served
            s.sendall(b"STATS\n")
            self.assertTrue(r.readline().startswith("OK"))
        c, rc = connect()
        self.assertEqual(rc.readline(), "ERR BUSY")
        c.close()
        a.close()
        for _ in range(50):                                # the freed slot becomes available
            d, rd = connect()
            d.sendall(b"STATS\n")
            if rd.readline().startswith("OK"):
                break
            d.close()
        else:
            self.fail("slot never freed")
        for s in (b, d):
            s.close()
        srv.close()


if __name__ == "__main__":
    unittest.main()
