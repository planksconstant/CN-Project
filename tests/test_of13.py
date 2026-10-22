import struct, unittest
from controller import of13


class T(unittest.TestCase):
    def test_hello(self):
        self.assertEqual(of13.hello(7), bytes([4, 0, 0, 8, 0, 0, 0, 7]))

    def test_match_padded(self):
        m = of13.match(ipv4_dst="10.0.0.2")
        self.assertEqual(len(m) % 8, 0)
        f, used = of13.parse_match(m)
        self.assertEqual(used, len(m))
        self.assertEqual(f[of13.OXM_ETH_TYPE], b"\x08\x00")
        self.assertEqual(f[of13.OXM_IPV4_DST], bytes([10, 0, 0, 2]))

    def test_empty_match(self):
        self.assertEqual(of13.match(), b"\x00\x01\x00\x04\x00\x00\x00\x00")

    def test_flow_mod_len(self):
        fm = of13.flow_mod(match_=of13.match(ipv4_dst="10.0.0.2"),
                           instructions=[of13.inst_meter(1),
                                         of13.inst_apply([of13.act_output(3)])])
        ver, t, ln, _ = struct.unpack("!BBHI", fm[:8])
        self.assertEqual((ver, t, ln), (4, of13.OFPT_FLOW_MOD, len(fm)))
        self.assertEqual(len(fm) % 8, 0)

    def test_meter_mod_len(self):
        self.assertEqual(len(of13.meter_mod(1, 2000, 100)), 8 + 8 + 16)


if __name__ == "__main__":
    unittest.main()
