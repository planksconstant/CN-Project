import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest

from controller.resmgr import ResourceManager, diamond
from controller.server import ReservationServer
from controller.service import ReservationService
from eval import iperf, live, sim, workload

HAVE_MPL = importlib.util.find_spec("matplotlib") is not None


class TestWorkload(unittest.TestCase):
    def test_deterministic_and_ordered(self):
        a, b = workload.generate(100, 2.0, seed=3), workload.generate(100, 2.0, seed=3)
        self.assertEqual(a, b)
        self.assertNotEqual(a, workload.generate(100, 2.0, seed=4))
        ts = [r["t"] for r in a]
        self.assertEqual(ts, sorted(ts))

    def test_rate_matches_load(self):
        rate = workload.rate_for_load(1.0)
        self.assertAlmostEqual(rate * workload.MEAN_BW * 17.5, workload.CROSS_CAPACITY_KBPS, delta=1)

    def test_requests_are_cross_switch(self):
        for r in workload.generate(300, 1.0, seed=2):
            if not r["bad"]:
                self.assertNotEqual(r["src"] in workload.LEFT, r["dst"] in workload.LEFT)


class TestSim(unittest.TestCase):
    def test_accounting_and_determinism(self):
        r = sim.run_one(1.0, 1, None, 300)
        self.assertEqual(r["accepted"] + r["rej_capacity"] + r["rej_conflict"] +
                         r["rej_malformed"] + r["rej_other"], 300)
        r2 = sim.run_one(1.0, 1, None, 300)
        self.assertEqual((r["accepted"], r["util_mean"], r["volume_ratio"]),
                         (r2["accepted"], r2["util_mean"], r2["volume_ratio"]))
        self.assertTrue(0 <= r["util_mean"] <= 1 and 0 <= r["accept_ratio"] <= 1)

    def test_two_paths_beat_one_under_load(self):
        acc = lambda mp: sum(sim.run_one(1.0, s, mp, 300)["accept_ratio"] for s in (1, 2, 3))
        self.assertGreater(acc(None), acc(1))

    def test_light_load_accepts_nearly_everything_valid(self):
        r = sim.run_one(0.1, 1, None, 300)
        self.assertEqual(r["rej_capacity"], 0)

    def test_max_paths_limits_path_choice(self):
        t = diamond(reservable=0.8)
        for mp, second_ok in ((None, True), (1, False)):
            m = ResourceManager(t, max_paths=mp)
            self.assertTrue(m.reserve("10.0.0.1", "10.0.0.2", 8000, 0, 10, now=0).ok)
            self.assertEqual(m.reserve("10.0.0.3", "10.0.0.4", 8000, 0, 10, now=0).ok, second_ok)


class TestIperf(unittest.TestCase):
    def test_both_layouts(self):
        new = json.dumps({"end": {"sum_received": {"bits_per_second": 3.9e6}, "sum": {"bits_per_second": 8e6}}})
        old = json.dumps({"end": {"sum": {"bits_per_second": 8e6, "lost_percent": 51.0}}})
        self.assertAlmostEqual(iperf.received_mbps(new), 3.9)
        self.assertAlmostEqual(iperf.received_mbps(old), 3.92)
        self.assertIsNone(iperf.received_mbps('{"error": "unable to connect"}'))
        self.assertIsNone(iperf.received_mbps("not json"))


class TestLive(unittest.TestCase):
    def test_burst_never_oversubscribes(self):
        mgr = ResourceManager(diamond(reservable=0.8), unique_flows=True)

        class Null:
            install = remove = lambda self, r: None
        srv = ReservationServer(ReservationService(mgr, Null()), "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as d:
            argv, buf = sys.argv, io.StringIO()
            sys.argv = ["live", "--port", str(srv.port), "--burst", "40", "--out", os.path.join(d, "l.csv")]
            try:
                with contextlib.redirect_stdout(buf):
                    live.main()
            finally:
                sys.argv = argv
                srv.close()
            out = buf.getvalue()
            self.assertIn("never oversubscribed", out)
            self.assertIn("requests 40", out)
            with open(os.path.join(d, "l.csv")) as f:
                self.assertEqual(len(f.readlines()), 41)


@unittest.skipUnless(HAVE_MPL, "matplotlib not installed")
class TestPlot(unittest.TestCase):
    def test_pngs(self):
        import csv
        from eval import plot
        with tempfile.TemporaryDirectory() as d:
            rows = [sim.run_one(rho, 1, mp, 150) for mp in (1, None) for rho in (0.5, 1.5)]
            p = os.path.join(d, "sim.csv")
            with open(p, "w", newline="") as f:
                w = csv.DictWriter(f, sim.FIELDS)
                w.writeheader()
                w.writerows(rows)
            plot.plot_sim(p, os.path.join(d, "sim.png"))
            u = os.path.join(d, "util.csv")
            with open(u, "w") as f:
                f.write("ts,link,reserved_kbps,cap_kbps,measured_kbps\n"
                        "1.0,s1-s2,0,8000,\n3.0,s1-s2,4000,8000,3900.0\n5.0,s1-s2,4000,8000,3950.5\n")
            plot.plot_util(u, "s1-s2", os.path.join(d, "util.png"))
            self.assertGreater(os.path.getsize(os.path.join(d, "sim.png")), 5000)
            self.assertGreater(os.path.getsize(os.path.join(d, "util.png")), 3000)


if __name__ == "__main__":
    unittest.main()
