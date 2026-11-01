import importlib
import sys
import types
import unittest

FAKE = ["mininet", "mininet.net", "mininet.node", "mininet.link", "mininet.cli", "mininet.log"]


class TestTestbedMatchesModel(unittest.TestCase):
    """topo/diamond.py is generated from controller.resmgr.diamond(); check what it would ask Mininet for."""

    def setUp(self):
        self.saved = {n: sys.modules.get(n) for n in FAKE + ["topo.diamond"]}
        self.calls = calls = []

        class Mininet:
            def __init__(self, **kw):
                calls.append(("net", kw["autoStaticArp"], kw["autoSetMacs"]))

            def addController(self, name, **kw):
                calls.append(("controller", name, kw["ip"], kw["port"]))

            def addSwitch(self, name, **kw):
                calls.append(("switch", name, kw))
                return name

            def addHost(self, name, **kw):
                calls.append(("host", name, kw))
                return name

            def addLink(self, a, b, **kw):
                calls.append(("link", a, b, kw))

        mods = {n: types.ModuleType(n) for n in FAKE}
        mods["mininet.net"].Mininet = Mininet
        mods["mininet.node"].RemoteController = mods["mininet.node"].OVSSwitch = object
        mods["mininet.link"].TCLink = object
        mods["mininet.cli"].CLI = object
        mods["mininet.log"].setLogLevel = lambda *_: None
        sys.modules.update(mods)
        sys.modules.pop("topo.diamond", None)

    def tearDown(self):
        for n, m in self.saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m

    def test_same_calls_as_the_original_hand_written_script(self):
        importlib.import_module("topo.diamond").build()
        sw = lambda i: ("switch", "s%d" % i, dict(dpid="%016x" % i, protocols="OpenFlow13", failMode="secure"))
        ho = lambda i: ("host", "h%d" % i, dict(ip="10.0.0.%d/24" % i, mac="00:00:00:00:00:%02x" % i))
        expected = [("net", True, False), ("controller", "c0", "127.0.0.1", 6653),
                    sw(1), sw(2), sw(3), sw(4), ho(1), ho(2), ho(3), ho(4),
                    ("link", "h1", "s1", {"port2": 1}), ("link", "h3", "s1", {"port2": 2}),
                    ("link", "h2", "s4", {"port2": 1}), ("link", "h4", "s4", {"port2": 2}),
                    ("link", "s1", "s2", {"port1": 3, "port2": 1, "bw": 10}),
                    ("link", "s1", "s3", {"port1": 4, "port2": 1, "bw": 10}),
                    ("link", "s2", "s4", {"port1": 2, "port2": 3, "bw": 10}),
                    ("link", "s3", "s4", {"port1": 2, "port2": 4, "bw": 10})]
        self.assertEqual(self.calls, expected)

    def test_reservable_share_does_not_change_the_testbed(self):
        from controller.resmgr import diamond
        mod = importlib.import_module("topo.diamond")
        mod.build(diamond(core_kbps=10000, reservable=0.5))
        bws = [c[3]["bw"] for c in self.calls if c[0] == "link" and "bw" in c[3]]
        self.assertEqual(bws, [10, 10, 10, 10])        # physical rate, not the reservable 5 Mbps


if __name__ == "__main__":
    unittest.main()
