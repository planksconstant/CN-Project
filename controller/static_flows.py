"""Step 3/4: hardcoded h1 <-> h2 path via s2, with an optional meter on h1->h2 at s1."""
from . import of13

H1, H2 = "10.0.0.1", "10.0.0.2"
METER_KBPS = 2000      # set to None to disable metering
METER_BURST_KB = 100
METER_ID = 1

# dpid -> {dst_ip: out_port}   (port numbers match topo/diamond.py)
STATIC = {
    1: {H2: 3, H1: 1},
    2: {H2: 2, H1: 1},
    4: {H2: 1, H1: 3},
}


def install(sw):
    # table-miss -> controller
    sw.send(of13.flow_mod(
        priority=0,
        instructions=[of13.inst_apply([of13.act_output(of13.OFPP_CONTROLLER)])],
        xid=sw.xid()))

    # meter must exist before a flow references it
    if METER_KBPS and sw.dpid == 1:
        sw.send(of13.meter_mod(METER_ID, METER_KBPS, METER_BURST_KB, xid=sw.xid()))

    for dst, port in STATIC.get(sw.dpid, {}).items():
        insts = []
        if METER_KBPS and sw.dpid == 1 and dst == H2:
            insts.append(of13.inst_meter(METER_ID))     # meter first, then actions
        insts.append(of13.inst_apply([of13.act_output(port)]))
        sw.send(of13.flow_mod(
            match_=of13.match(ipv4_dst=dst),
            instructions=insts,
            priority=100, xid=sw.xid()))
    sw.send(of13.barrier_request(sw.xid()))
