#!/usr/bin/env python3
"""
Diamond topology, fixed port numbers so the controller can hardcode them.

            +--- s2 ---+
  h1,h3 - s1          s4 - h2,h4
            +--- s3 ---+

Ports:
  s1: 1=h1 2=h3 3=s2 4=s3
  s2: 1=s1 2=s4
  s3: 1=s1 2=s4
  s4: 1=h2 2=h4 3=s2 4=s3
Run:  sudo python3 topo/diamond.py     (controller must already be running)
"""
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel

LINK_MBPS = 10


def build():
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink,
                  autoSetMacs=False, autoStaticArp=True)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)

    sw = {n: net.addSwitch(n, dpid="%016x" % i, protocols="OpenFlow13", failMode="secure")
          for i, n in ((1, "s1"), (2, "s2"), (3, "s3"), (4, "s4"))}
    h = {i: net.addHost("h%d" % i, ip="10.0.0.%d/24" % i, mac="00:00:00:00:00:%02x" % i)
         for i in (1, 2, 3, 4)}

    # hosts (access links)
    net.addLink(h[1], sw["s1"], port2=1)
    net.addLink(h[3], sw["s1"], port2=2)
    net.addLink(h[2], sw["s4"], port2=1)
    net.addLink(h[4], sw["s4"], port2=2)
    # core links, capacity = LINK_MBPS
    bw = dict(bw=LINK_MBPS)
    net.addLink(sw["s1"], sw["s2"], port1=3, port2=1, **bw)
    net.addLink(sw["s1"], sw["s3"], port1=4, port2=1, **bw)
    net.addLink(sw["s2"], sw["s4"], port1=2, port2=3, **bw)
    net.addLink(sw["s3"], sw["s4"], port1=2, port2=4, **bw)
    return net


if __name__ == "__main__":
    setLogLevel("info")
    net = build()
    net.start()
    CLI(net)
    net.stop()
