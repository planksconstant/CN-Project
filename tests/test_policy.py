import collections
import csv
import os
import struct
import tempfile
import unittest

from controller import of13, static_flows
from controller.installer import GRACE, PRIO_FLOW, PRIO_PAIR
from controller.installer import OFInstaller
from controller.resmgr import ResourceManager, diamond
from controller.service import ReservationService
from controller.stats import StatsPoller

H1, H2, H3, H4 = ("10.0.0.%d" % i for i in (1, 2, 3, 4))
ALL = 0xFFFFFFFFFFFFFFFF


# ------------------------------------------------------------------ helpers
class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSwitch:
    def __init__(self, dpid):
        self.dpid, self.ready = dpid, True
        self.sent, self.errors = [], collections.deque()
        self._x, self.barrier_ok, self.fail_flow = 0, True, False

    def xid(self):
        self._x += 1
        return self._x

    def send(self, data):
        self.sent.append(data)
        _v, t, _l, xid = struct.unpack("!BBHI", data[:8])
        if self.fail_flow and t == of13.OFPT_FLOW_MOD:
            self.errors.append((xid, 5, 0))

    def barrier(self, timeout=1.0):
        return self.barrier_ok


def parse_insts(b):
    out, off = [], 0
    while off < len(b):
        it, ln = struct.unpack_from("!HH", b, off)
        if it == of13.OFPIT_METER:
            out.append(("meter", struct.unpack_from("!I", b, off + 4)[0]))
        elif it == of13.OFPIT_APPLY_ACTIONS:
            ports, a = [], off + 8
            while a < off + ln:
                _t, al, port = struct.unpack_from("!HHI", b, a)
                ports.append(port)
                a += al
            out.append(("apply", ports))
        off += ln
    return out


def decode(msg):
    _v, t, ln, xid = struct.unpack("!BBHI", msg[:8])
    assert ln == len(msg) and ln % 8 == 0
    body = msg[8:]
    if t == of13.OFPT_FLOW_MOD:
        cookie, cmask, table, cmd, idle, hard, prio, _b, _op, _og, _fl = \
            struct.unpack_from("!QQBBHHHIIIH", body, 0)
        fields, used = of13.parse_match(body, 40)
        return dict(kind="flow", cookie=cookie, cmask=cmask, table=table, cmd=cmd, hard=hard,
                    prio=prio, fields=fields, insts=parse_insts(body[40 + used:]))
    if t == of13.OFPT_METER_MOD:
        cmd, flags, mid = struct.unpack_from("!HHI", body, 0)
        d = dict(kind="meter", cmd=cmd, flags=flags, id=mid)
        if len(body) > 8:
            _bt, _bl, d["rate"], d["burst"] = struct.unpack_from("!HHII", body, 8)
        return d
    return dict(kind="other", type=t)


def flows(sw, **kw):
    return [d for d in map(decode, sw.sent) if d["kind"] == "flow"
            and all(d[k] == v for k, v in kw.items())]


def build(reservable=0.8):
    topo = diamond(core_kbps=10000, reservable=reservable)
    mgr = ResourceManager(topo, unique_flows=True)
    sws = {d: FakeSwitch(d) for d in (1, 2, 3, 4)}
    clk = Clock()
    svc = ReservationService(mgr, OFInstaller(topo, sws.get, clk), clk)
    return topo, mgr, sws, svc, clk


# ------------------------------------------------------------------ installer
class TestInstaller(unittest.TestCase):
    def test_flow_reservation_messages(self):
        _, _, sws, svc, _ = build()
        self.assertEqual(svc.handle("RESERVE %s %s 4000 0 60 udp/5201" % (H1, H2)),
                         ["OK 1 h1-s1-s2-s4-h2"])
        m, f = map(decode, sws[1].sent)                    # ingress: meter first, then flow
        self.assertEqual((m["kind"], m["cmd"], m["id"], m["rate"]), ("meter", 0, 1, 4000))
        self.assertEqual(m["flags"], of13.OFPMF_KBPS | of13.OFPMF_BURST | of13.OFPMF_STATS)
        self.assertEqual((f["cookie"], f["prio"], f["cmd"]), (1, PRIO_FLOW, 0))
        self.assertEqual(f["hard"], 60 + GRACE)
        self.assertEqual(f["fields"][of13.OXM_IPV4_SRC], bytes([10, 0, 0, 1]))
        self.assertEqual(f["fields"][of13.OXM_IPV4_DST], bytes([10, 0, 0, 2]))
        self.assertEqual(f["fields"][of13.OXM_IP_PROTO], b"\x11")
        self.assertEqual(f["fields"][of13.OXM_UDP_DST], struct.pack("!H", 5201))
        self.assertEqual(f["insts"], [("meter", 1), ("apply", [3])])
        (f2,), (f4,) = flows(sws[2]), flows(sws[4])
        self.assertEqual((f2["insts"], f4["insts"]), ([("apply", [2])], [("apply", [1])]))
        self.assertEqual(sws[3].sent, [])

    def test_pair_reservation_is_lower_priority_and_has_no_l4(self):
        _, _, sws, svc, _ = build()
        svc.handle("RESERVE %s %s 4000 0 60" % (H1, H2))
        (f,) = flows(sws[1])
        self.assertEqual(f["prio"], PRIO_PAIR)
        self.assertNotIn(of13.OXM_UDP_DST, f["fields"])
        self.assertLess(PRIO_PAIR, PRIO_FLOW)
        self.assertGreater(PRIO_PAIR, static_flows.PRIO_BE_INGRESS)

    def test_cancel_deletes_by_cookie_and_meter(self):
        _, _, sws, svc, _ = build()
        svc.handle("RESERVE %s %s 4000 0 60 udp/5201" % (H1, H2))
        self.assertEqual(svc.handle("CANCEL 1"), ["OK"])
        d, m = map(decode, sws[1].sent[-2:])
        self.assertEqual((d["cmd"], d["cookie"], d["cmask"], d["table"]), (3, 1, ALL, 0xFF))
        self.assertEqual((m["kind"], m["cmd"], m["id"]), ("meter", 2, 1))
        for dp in (2, 4):
            d = decode(sws[dp].sent[-1])
            self.assertEqual((d["cmd"], d["cookie"]), (3, 1))
        self.assertEqual(sws[3].sent, [])

    def test_expiry_removes_policy(self):
        _, _, sws, svc, clk = build()
        svc.handle("RESERVE %s %s 4000 0 30 udp/5201" % (H1, H2))
        clk.t += 31
        svc.sync()
        self.assertEqual(decode(sws[1].sent[-2])["cmd"], 3)

    def test_second_flow_takes_alternate_path(self):
        _, _, sws, svc, _ = build()
        self.assertEqual(svc.handle("RESERVE %s %s 6000 0 60 udp/5201" % (H1, H2))[0][:4], "OK 1")
        self.assertEqual(svc.handle("RESERVE %s %s 6000 0 60 udp/5202" % (H1, H2)),
                         ["OK 2 h1-s1-s3-s4-h2"])
        self.assertEqual(flows(sws[1], cookie=2)[0]["insts"][-1], ("apply", [4]))
        self.assertEqual(len(flows(sws[3])), 1)

    def test_missing_switch_rejects_and_frees_capacity(self):
        _, mgr, sws, svc, _ = build()
        sw2 = sws.pop(2)
        self.assertEqual(svc.handle("RESERVE %s %s 4000 0 60 udp/5201" % (H1, H2)),
                         ["REJECT INSTALL_FAILED"])
        self.assertTrue(all(not s.sent for s in sws.values()))        # nothing half-installed
        self.assertEqual(mgr.get(1).state, "CANCELLED")
        sws[2] = sw2
        self.assertEqual(svc.handle("RESERVE %s %s 4000 0 60 udp/5201" % (H1, H2))[0][:4], "OK 2")
        self.assertEqual((mgr.counts["accepted"], mgr.counts["reason:INSTALL_FAILED"]), (1, 1))

    def test_switch_error_rolls_back(self):
        _, _, sws, svc, _ = build()
        sws[2].fail_flow = True
        self.assertEqual(svc.handle("RESERVE %s %s 4000 0 60 udp/5201" % (H1, H2)),
                         ["REJECT INSTALL_FAILED"])
        self.assertTrue(any(d["cmd"] == 3 and d["cookie"] == 1 for d in flows(sws[4])))

    def test_barrier_timeout_rejects(self):
        _, _, sws, svc, _ = build()
        sws[4].barrier_ok = False
        self.assertEqual(svc.handle("RESERVE %s %s 4000 0 60 udp/5201" % (H1, H2)),
                         ["REJECT INSTALL_FAILED"])


# ------------------------------------------------------------------ flow identity
class TestFlowIdentity(unittest.TestCase):
    def setUp(self):
        self.m = ResourceManager(diamond(core_kbps=10000), unique_flows=True)

    def test_conflicts(self):
        r = self.m.reserve
        self.assertTrue(r(H1, H2, 100, 0, 10, now=0).ok)
        self.assertEqual(r(H1, H2, 100, 5, 15, now=0).reason, "FLOW_CONFLICT")   # same pair, overlap
        self.assertTrue(r(H1, H2, 100, 10, 20, now=0).ok)                         # adjacent is fine
        self.assertTrue(r(H1, H2, 100, 0, 10, now=0, flow=("udp", 5201)).ok)      # more specific
        self.assertEqual(r(H1, H2, 100, 0, 10, now=0, flow=("udp", 5201)).reason, "FLOW_CONFLICT")
        self.assertTrue(r(H1, H2, 100, 0, 10, now=0, flow=("tcp", 5201)).ok)
        self.assertTrue(r(H3, H2, 100, 0, 10, now=0, flow=("udp", 5201)).ok)

    def test_bad_flow(self):
        for fl in (("udp", 0), ("udp", 70000), ("icmp", 5)):
            self.assertEqual(self.m.reserve(H1, H2, 100, 0, 10, now=0, flow=fl).reason, "BAD_REQUEST")

    def test_protocol_flow_arg(self):
        _, _, _, svc, _ = build()
        self.assertEqual(svc.handle("RESERVE %s %s 100 0 10 udp/0" % (H1, H2)), ["REJECT BAD_REQUEST"])
        for bad in ("udp", "udp/abc", "a/b/c"):
            self.assertEqual(svc.handle("RESERVE %s %s 100 0 10 %s" % (H1, H2, bad)),
                             ["REJECT MALFORMED"], bad)
        svc.handle("RESERVE %s %s 100 0 10 udp/5201" % (H1, H2))
        self.assertTrue(svc.handle("STATUS 1")[0].endswith("udp/5201"))

    def test_reservable_share(self):
        t = diamond(core_kbps=10000, reservable=0.8)
        self.assertEqual((t.cap[("s1", "s2")], t.be_kbps), (8000, 2000))
        m = ResourceManager(t)
        self.assertTrue(m.reserve(H1, H2, 8000, 0, 10, now=0).ok)
        self.assertTrue(m.reserve(H3, H4, 8000, 0, 10, now=0).ok)     # alternate path
        self.assertEqual(m.reserve(H1, H4, 100, 0, 10, now=0).reason, "CAPACITY")


# ------------------------------------------------------------------ static (best-effort) flows
class TestStaticFlows(unittest.TestCase):
    def test_s1_rules(self):
        topo = diamond(core_kbps=10000, reservable=0.8)
        sw = FakeSwitch(1)
        static_flows.install(sw, topo)
        msgs = [decode(m) for m in sw.sent]
        self.assertEqual(msgs[0]["cmd"], 3)                                  # wipe flows
        self.assertEqual((msgs[1]["kind"], msgs[1]["cmd"], msgs[1]["id"]), ("meter", 2, of13.OFPM_ALL))
        be = [m for m in msgs if m["kind"] == "meter" and m["cmd"] == 0]
        self.assertEqual((be[0]["id"], be[0]["rate"]), (static_flows.BE_METER_ID, 2000))
        dst = {f["fields"][of13.OXM_IPV4_DST][3]: f["insts"][0][1][0] for f in flows(sw, prio=100)}
        self.assertEqual(dst, {1: 1, 3: 2, 2: 3, 4: 3})                      # dst host -> out port
        ing = flows(sw, prio=110)
        self.assertEqual(len(ing), 6)                                        # h1,h3 -> 3 others each
        for f in ing:
            self.assertEqual(f["insts"][0], ("meter", static_flows.BE_METER_ID))
            self.assertIn(of13.OXM_IN_PORT, f["fields"])
        self.assertEqual(len(flows(sw, prio=0, cmd=0)), 1)

    def test_s3_has_no_best_effort_rules_and_s4_ingress(self):
        topo = diamond(core_kbps=10000, reservable=0.8)
        s3, s4 = FakeSwitch(3), FakeSwitch(4)
        static_flows.install(s3, topo)
        static_flows.install(s4, topo)
        self.assertEqual(len(flows(s3, prio=100)) + len(flows(s3, prio=110)), 0)
        self.assertEqual(len(flows(s4, prio=110)), 6)

    def test_no_meter_when_everything_reservable(self):
        topo = diamond(core_kbps=10000, reservable=1.0)
        sw = FakeSwitch(1)
        static_flows.install(sw, topo)
        self.assertFalse([m for m in map(decode, sw.sent) if m["kind"] == "meter" and m["cmd"] == 0])
        self.assertTrue(all(f["insts"][0][0] == "apply" for f in flows(sw, prio=110)))

    def test_wipe_only_when_asked(self):
        sw = FakeSwitch(2)
        static_flows.install(sw, diamond(), wipe=False)
        self.assertFalse(flows(sw, cmd=3))


# ------------------------------------------------------------------ multipart stats
def port_entry(port, tx_bytes):
    return struct.pack("!I4x12QII", port, 0, 0, 0, tx_bytes, 0, 0, 0, 0, 0, 0, 0, 0, 5, 0)


def meter_entry(mid, byte_in, bands):
    ln = 40 + 16 * len(bands)
    return struct.pack("!IH6xIQQII", mid, ln, 1, 0, byte_in, 3, 0) + \
        b"".join(struct.pack("!QQ", p, b) for p, b in bands)


class TestStats(unittest.TestCase):
    def test_parse_port_stats(self):
        data = port_entry(3, 12345) + port_entry(1, 7)
        self.assertEqual(struct.calcsize("!I4x12QII"), 112)
        ps = of13.parse_port_stats(data)
        self.assertEqual([(p["port"], p["tx_bytes"]) for p in ps], [(3, 12345), (1, 7)])

    def test_parse_meter_stats(self):
        ms = of13.parse_meter_stats(meter_entry(1, 5000, [(10, 900)]) + meter_entry(900000, 1, [(0, 0)]))
        self.assertEqual([(m["meter_id"], m["byte_in"], m["bands"]) for m in ms],
                         [(1, 5000, [(10, 900)]), (900000, 1, [(0, 0)])])

    def test_requests_are_well_formed(self):
        for msg, mp in ((of13.port_stats_request(xid=5), 4), (of13.meter_stats_request(xid=6), 9)):
            _v, t, ln, _x = struct.unpack("!BBHI", msg[:8])
            self.assertEqual((t, ln), (of13.OFPT_MULTIPART_REQUEST, len(msg)))
            self.assertEqual(struct.unpack_from("!HH", msg, 8)[0], mp)
            self.assertEqual(len(msg), 24)

    def test_poller_computes_rate_and_logs(self):
        topo = diamond(core_kbps=10000, reservable=0.8)
        mgr = ResourceManager(topo)
        mgr.reserve(H1, H2, 4000, 0, 100, now=0)
        sw = FakeSwitch(1)
        clk = Clock(0.0)
        with tempfile.TemporaryDirectory() as d:
            p = StatsPoller(topo, mgr, lambda: [sw], clock=clk, outdir=d)
            p.poll_once()
            self.assertEqual(len(sw.sent), 2)
            reply = lambda mp, data: struct.pack("!HH4x", mp, 0) + data
            p.on_reply(sw, reply(4, port_entry(3, 0)))
            p.log_cycle(now=1.0)
            clk.t = 2.0
            p.on_reply(sw, reply(4, port_entry(3, 250000)))                     # 250 kB in 2 s = 1000 kbps
            p.on_reply(sw, reply(9, meter_entry(1, 9000, [(5, 1200)])))
            p.log_cycle(now=3.0)
            p._uf.close()
            p._mf.close()
            with open(os.path.join(d, "util.csv")) as fh:
                rows = list(csv.DictReader(fh))
            last = [r for r in rows if r["link"] == "s1-s2"][-1]
            self.assertEqual((last["reserved_kbps"], last["cap_kbps"], last["measured_kbps"]),
                             ("4000", "8000", "1000.0"))
            self.assertEqual([r for r in rows if r["link"] == "s1-s2"][0]["measured_kbps"], "")
            with open(os.path.join(d, "meters.csv")) as fh:
                mrows = list(csv.DictReader(fh))
            self.assertEqual((mrows[-1]["meter"], mrows[-1]["bytes_in"], mrows[-1]["bytes_dropped"]),
                             ("1", "9000", "1200"))


if __name__ == "__main__":
    unittest.main()
