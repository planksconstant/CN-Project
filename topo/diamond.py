#!/usr/bin/env python3
"""
Mininet testbed built from the controller's own topology model (controller/resmgr.diamond),
so ports, IPs, dpids and link rates are defined in exactly one place.

            +--- s2 ---+
  h1,h3 - s1          s4 - h2,h4
            +--- s3 ---+

Ports:  s1: 1=h1 2=h3 3=s2 4=s3 | s2: 1=s1 2=s4 | s3: 1=s1 2=s4 | s4: 1=h2 2=h4 3=s2 4=s3
Run:    sudo python3 topo/diamond.py      (controller must already be running)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel

from controller.resmgr import diamond

LINK_MBPS = 10          # physical core link rate


def build(topo=None):
    """Mininet network for `topo` (a controller.resmgr.Topology); default: the diamond."""
    topo = topo or diamond(core_kbps=LINK_MBPS * 1000)
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink,
                  autoSetMacs=False, autoStaticArp=True)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)

    nodes = {}
    for name, dpid in topo.switches.items():
        nodes[name] = net.addSwitch(name, dpid="%016x" % dpid, protocols="OpenFlow13", failMode="secure")
    for ip, name in topo.hosts.items():
        nodes[name] = net.addHost(name, ip="%s/24" % ip,
                                  mac="00:00:00:00:00:%02x" % int(ip.split(".")[-1]))
    for u, v, port_u, port_v, shaped in topo.link_list:
        kw = {}
        if u in topo.switches:
            kw["port1"] = port_u
        if v in topo.switches:
            kw["port2"] = port_v
        if shaped:
            kw["bw"] = shaped / 1000.0
        net.addLink(nodes[u], nodes[v], **kw)
    return net


if __name__ == "__main__":
    setLogLevel("info")
    net = build()
    net.start()
    CLI(net)
    net.stop()
