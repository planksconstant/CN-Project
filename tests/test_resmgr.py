import threading
import unittest

from controller import resmgr
from controller.resmgr import ResourceManager, diamond

H1, H2, H3, H4 = ("10.0.0.%d" % i for i in (1, 2, 3, 4))


def mk():
    return ResourceManager(diamond(core_kbps=10000))


class T(unittest.TestCase):
    def test_accept_shortest_path(self):
        m = mk()
        r = m.reserve(H1, H2, 6000, 100, 200, now=0)
        self.assertTrue(r.ok)
        self.assertEqual(r.path, ["h1", "s1", "s2", "s4", "h2"])

    def test_falls_back_to_alternate_then_rejects(self):
        m = mk()
        self.assertEqual(m.reserve(H1, H2, 6000, 100, 200, now=0).path[2], "s2")
        self.assertEqual(m.reserve(H3, H4, 6000, 100, 200, now=0).path[2], "s3")
        r = m.reserve(H1, H2, 6000, 100, 200, now=0)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "CAPACITY")
        self.assertTrue(m.reserve(H1, H2, 6000, 200, 300, now=0).ok)   # later window is free

    def test_exact_capacity_and_over(self):
        m = mk()
        self.assertTrue(m.reserve(H1, H2, 10000, 100, 200, now=0).ok)
        self.assertEqual(m.reserve(H1, H2, 10001, 300, 400, now=0).reason, "CAPACITY")

    def test_adjacent_intervals_do_not_conflict(self):
        m = mk()
        self.assertTrue(m.reserve(H1, H2, 10000, 0, 10, now=0).ok)
        self.assertTrue(m.reserve(H1, H2, 10000, 10, 20, now=0).ok)

    def test_peak_not_sum(self):
        m = mk()
        # A[0,10)=6000, B[5,15)=3000 -> s2 load peaks at 9000 in [5,10)
        self.assertTrue(m.reserve(H1, H2, 6000, 0, 10, now=0).ok)
        self.assertTrue(m.reserve(H1, H2, 3000, 5, 15, now=0).ok)
        # C[12,20)=6000 only overlaps B (3000) -> 9000 fits, stays on s2
        self.assertEqual(m.reserve(H1, H2, 6000, 12, 20, now=0).path[2], "s2")
        # D[5,10)=4000 would hit 9000+4000 on s2 -> must go via s3
        self.assertEqual(m.reserve(H1, H2, 4000, 5, 10, now=0).path[2], "s3")

    def test_cancel_frees_capacity(self):
        m = mk()
        a = m.reserve(H1, H2, 10000, 100, 200, now=0)
        m.reserve(H3, H4, 10000, 100, 200, now=0)          # takes s3 path
        self.assertFalse(m.reserve(H1, H2, 10000, 100, 200, now=0).ok)
        self.assertIsNotNone(m.cancel(a.rid))
        self.assertTrue(m.reserve(H1, H2, 10000, 100, 200, now=0).ok)
        self.assertIsNone(m.cancel(a.rid))                  # already cancelled

    def test_lifecycle_and_utilization(self):
        m = mk()
        a = m.reserve(H1, H2, 6000, 100, 110, now=0)
        self.assertEqual(m.tick(50), ([], []))
        act, exp = m.tick(100)
        self.assertEqual([r.id for r in act], [a.rid])
        self.assertEqual(m.get(a.rid).state, resmgr.ACTIVE)
        self.assertEqual(m.utilization(105)[("s1", "s2")], (6000, 10000))
        act, exp = m.tick(110)
        self.assertEqual([r.id for r in exp], [a.rid])
        self.assertEqual(m.utilization(120)[("s1", "s2")][0], 0)

    def test_bad_requests(self):
        m = mk()
        bad = [(H1, H2, 0, 100, 200), (H1, H2, -5, 100, 200), (H1, H2, 100, 200, 100),
               (H1, H1, 100, 100, 200), (H1, H2, 100, 0, 5)]
        for a in bad:
            self.assertEqual(m.reserve(*a, now=10).reason, "BAD_REQUEST", a)
        self.assertEqual(m.reserve("1.2.3.4", H2, 100, 100, 200, now=0).reason, "UNKNOWN_HOST")

    def test_start_in_past_is_clamped(self):
        m = mk()
        r = m.reserve(H1, H2, 1000, 0, 100, now=50)
        self.assertEqual(m.get(r.rid).start, 50)

    def test_directions_independent(self):
        m = mk()
        self.assertTrue(m.reserve(H1, H2, 10000, 100, 200, now=0).ok)
        r = m.reserve(H2, H1, 10000, 100, 200, now=0)
        self.assertTrue(r.ok)
        self.assertEqual(r.path[2], "s2")

    def test_switch_hops(self):
        m = mk()
        r = m.reserve(H1, H2, 1000, 100, 200, now=0)
        self.assertEqual(m.topo.switch_hops(r.path), [(1, 1, 3), (2, 1, 2), (4, 3, 1)])
        r = m.reserve(H2, H1, 1000, 100, 200, now=0)
        self.assertEqual(m.topo.switch_hops(r.path), [(4, 1, 3), (2, 2, 1), (1, 3, 1)])

    def test_concurrent_never_oversubscribes(self):
        m = mk()
        results = []

        def worker():
            for _ in range(20):
                results.append(m.reserve(H1, H2, 1000, 100, 200, now=0).ok)

        ts = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(results), 20)                  # 2 paths x 10 Mbps / 1 Mbps
        for l, (used, cap) in m.utilization(150).items():
            self.assertLessEqual(used, cap)


if __name__ == "__main__":
    unittest.main()
