import socket
import threading
import unittest

from controller.resmgr import ResourceManager, diamond
from controller.server import LineReader, ReservationServer
from controller.service import LogInstaller, ReservationService

H1, H2, H3, H4 = ("10.0.0.%d" % i for i in (1, 2, 3, 4))


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def mk():
    clk = Clock()
    mgr = ResourceManager(diamond(core_kbps=10000))
    inst = LogInstaller(mgr.topo)
    return ReservationService(mgr, inst, clk), inst, clk


class TestService(unittest.TestCase):
    def test_immediate_reserve_installs(self):
        s, inst, _ = mk()
        self.assertEqual(s.handle("RESERVE %s %s 6000 0 30" % (H1, H2)), ["OK 1 h1-s1-s2-s4-h2"])
        self.assertEqual(inst.installed, [1])

    def test_future_then_expiry(self):
        s, inst, clk = mk()
        s.handle("RESERVE %s %s 6000 10 20" % (H1, H2))
        self.assertEqual(inst.installed, [])
        clk.t += 10
        s.sync()
        self.assertEqual(inst.installed, [1])
        clk.t += 20
        s.sync()
        self.assertEqual(inst.removed, [1])
        self.assertTrue(s.handle("STATUS 1")[0].startswith("OK 1 EXPIRED"))

    def test_capacity_reject(self):
        s, inst, _ = mk()
        self.assertTrue(s.handle("RESERVE %s %s 10000 0 30" % (H1, H2))[0].startswith("OK"))
        self.assertTrue(s.handle("RESERVE %s %s 10000 0 30" % (H1, H2))[0].startswith("OK"))
        self.assertEqual(s.handle("RESERVE %s %s 1 0 30" % (H1, H2)), ["REJECT CAPACITY"])

    def test_malformed(self):
        s, _, _ = mk()
        for l in ["RESERVE a b", "RESERVE %s %s abc 0 5" % (H1, H2),
                  "RESERVE %s %s nan 0 5" % (H1, H2), "RESERVE %s %s 100 0 inf" % (H1, H2)]:
            self.assertEqual(s.handle(l), ["REJECT MALFORMED"], l)
        self.assertEqual(s.handle("RESERVE %s %s -5 0 5" % (H1, H2)), ["REJECT BAD_REQUEST"])
        self.assertEqual(s.handle("RESERVE 9.9.9.9 %s 5 0 5" % H2), ["REJECT UNKNOWN_HOST"])
        self.assertEqual(s.handle("FROB"), ["ERR BAD_COMMAND"])
        self.assertIn("reason:MALFORMED=4", s.handle("STATS")[0])

    def test_cancel(self):
        s, inst, _ = mk()
        s.handle("RESERVE %s %s 1000 0 30" % (H1, H2))        # active
        s.handle("RESERVE %s %s 1000 50 30" % (H1, H2))       # pending
        self.assertEqual(s.handle("CANCEL 2"), ["OK"])
        self.assertEqual(inst.removed, [])                      # never installed
        self.assertEqual(s.handle("CANCEL 1"), ["OK"])
        self.assertEqual(inst.removed, [1])
        self.assertEqual(s.handle("CANCEL 1"), ["ERR NOT_CANCELLABLE"])
        self.assertEqual(s.handle("CANCEL 99"), ["ERR NOT_FOUND"])
        self.assertEqual(s.handle("CANCEL x"), ["ERR BAD_ARGS"])

    def test_list_util_stats(self):
        s, _, _ = mk()
        s.handle("RESERVE %s %s 6000 0 30" % (H1, H2))
        out = s.handle("LIST")
        self.assertEqual(out[0], "OK 1")
        self.assertEqual(out[-1], "END")
        self.assertIn("s1-s2 6000 10000 60.0", s.handle("UTIL"))
        self.assertEqual(s.handle("STATS"), ["OK accepted=1 rejected=0"])


class TestTCP(unittest.TestCase):
    def setUp(self):
        self.svc, self.inst, _ = mk()
        self.srv = ReservationServer(self.svc, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.close()

    def conn(self):
        s = socket.create_connection(("127.0.0.1", self.srv.port))
        s.settimeout(5)
        return s, LineReader(s)

    def test_partial_writes_and_crlf(self):
        s, rd = self.conn()
        s.sendall(b"RESE")
        s.sendall(("RVE %s %s 1000 0 30\r\n" % (H1, H2)).encode())
        self.assertTrue(rd.readline().startswith("OK 1"))
        s.sendall(b"STATS\nQUIT\n")                             # two commands in one segment
        self.assertTrue(rd.readline().startswith("OK accepted=1"))
        self.assertEqual(rd.readline(), "OK BYE")
        self.assertIsNone(rd.readline())                        # server closed
        s.close()

    def test_line_too_long(self):
        s, rd = self.conn()
        s.sendall(b"A" * 10000)
        self.assertEqual(rd.readline(), "ERR LINE_TOO_LONG")
        s.close()

    def test_concurrent_clients_never_oversubscribe(self):
        accepted = []

        def client():
            s, rd = self.conn()
            for _ in range(10):
                s.sendall(("RESERVE %s %s 1000 0 100\n" % (H1, H2)).encode())
                accepted.append(rd.readline().startswith("OK"))
            s.close()

        ts = [threading.Thread(target=client) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(accepted), 80)
        self.assertEqual(sum(accepted), 20)
        self.assertEqual(len(self.inst.installed), 20)


if __name__ == "__main__":
    unittest.main()
