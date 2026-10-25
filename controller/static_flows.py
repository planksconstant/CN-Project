"""
Best-effort forwarding for every host pair, installed when a switch connects.
Reservations sit above it (priority 200/210), so reserved traffic never lands here.

  prio 0    table-miss -> controller (debug visibility)
  prio 100  match ipv4_dst            -> output (all switches on the shortest path)
  prio 110  match in_port + ipv4_dst  -> BE meter, output (ingress switch only)

The BE meter caps all unreserved traffic entering a switch at topo.be_kbps,
the share of the core link that admission control never hands out.
"""
from . import of13

PRIO_MISS, PRIO_BE, PRIO_BE_INGRESS = 0, 100, 110
BE_METER_ID = 900000


def routes(topo):
    """-> (next_hop {(dpid, dst_ip): out_port}, ingress [(dpid, in_port, dst_ip, out_port)])"""
    nh, ingress = {}, []
    for a_ip, a in topo.hosts.items():
        for b_ip, b in topo.hosts.items():
            if a == b:
                continue
            p = topo.paths(a, b)[0]                       # shortest path, deterministic tie-break
            for i in range(1, len(p) - 1):
                nh.setdefault((topo.switches[p[i]], b_ip), topo.port[(p[i], p[i + 1])])
            ingress.append((topo.switches[p[1]], topo.port[(p[1], a)], b_ip, topo.port[(p[1], p[2])]))
    return nh, ingress


def install(sw, topo, wipe=True):
    if wipe:                     # clean slate: drop flows/meters left over from a previous controller run
        sw.send(of13.flow_mod(command=of13.OFPFC_DELETE, table_id=of13.OFPTT_ALL, xid=sw.xid()))
        sw.send(of13.meter_mod(of13.OFPM_ALL, command=of13.OFPMC_DELETE, xid=sw.xid()))

    sw.send(of13.flow_mod(
        priority=PRIO_MISS,
        instructions=[of13.inst_apply([of13.act_output(of13.OFPP_CONTROLLER)])],
        xid=sw.xid()))

    be = topo.be_kbps
    if be > 0:                   # meter must exist before flows reference it
        sw.send(of13.meter_mod(BE_METER_ID, be, max(16, be // 10), xid=sw.xid()))

    nh, ingress = routes(topo)
    for (dpid, dst), port in nh.items():
        if dpid == sw.dpid:
            sw.send(of13.flow_mod(
                match_=of13.match(ipv4_dst=dst),
                instructions=[of13.inst_apply([of13.act_output(port)])],
                priority=PRIO_BE, xid=sw.xid()))
    for dpid, in_port, dst, port in ingress:
        if dpid == sw.dpid:
            insts = [of13.inst_meter(BE_METER_ID)] if be > 0 else []
            insts.append(of13.inst_apply([of13.act_output(port)]))
            sw.send(of13.flow_mod(
                match_=of13.match(in_port=in_port, ipv4_dst=dst),
                instructions=insts, priority=PRIO_BE_INGRESS, xid=sw.xid()))
    sw.send(of13.barrier_request(sw.xid()))     # not waited on: we're on the reader thread
