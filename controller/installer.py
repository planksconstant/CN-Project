"""
Policy installer: ACTIVE reservation -> OpenFlow state on the switches of its path.

Per reservation (id = rid):
  ingress switch : METER_MOD(rid, rate)  +  FLOW_MOD(match -> meter rid, output)
  other switches : FLOW_MOD(match -> output)
All flows carry cookie=rid, so removal is "delete by cookie". Flows get a hard_timeout a few
seconds past the reservation end as a safety net if the controller dies.
Must run on a non-reader thread (it waits for barrier replies).
"""
import logging
import math

from . import of13

log = logging.getLogger("inst")
PRIO_PAIR, PRIO_FLOW = 200, 210      # host-pair reservation / single-flow (more specific) reservation
GRACE = 5                            # seconds the hard_timeout outlives the reservation
ALL_COOKIE = 0xFFFFFFFFFFFFFFFF


class InstallError(Exception):
    pass


class OFInstaller:
    def __init__(self, topo, get_switch, clock):
        self.topo, self.get_switch, self.clock = topo, get_switch, clock

    def _match(self, r):
        kw = {}
        if r.flow:
            proto, port = r.flow
            kw["tcp_dst" if proto == "tcp" else "udp_dst"] = port
        return of13.match(ipv4_src=r.src_ip, ipv4_dst=r.dst_ip, **kw)

    def install(self, r):
        hops = self.topo.switch_hops(r.path)                  # [(dpid, in_port, out_port)]
        sws = []
        for dpid, _, _ in hops:                               # check first, so we don't half-install
            sw = self.get_switch(dpid)
            if sw is None or not sw.ready:
                raise InstallError("switch %d not connected" % dpid)
            sws.append(sw)

        prio = PRIO_FLOW if r.flow else PRIO_PAIR
        match = self._match(r)
        hard = int(math.ceil(r.end - self.clock())) + GRACE
        if hard > 0xFFFF:
            hard = 0                                          # too long for the 16-bit field: no safety net
        rate = max(1, int(round(r.kbps)))
        sent = [[] for _ in hops]                             # xids we used, per hop

        try:
            for i in reversed(range(len(hops))):              # egress first, ingress last
                sw, out = sws[i], hops[i][2]
                insts = []
                if i == 0:
                    x = sw.xid()
                    sent[i].append(x)
                    sw.send(of13.meter_mod(r.id, rate, max(16, rate // 10), xid=x))
                    insts.append(of13.inst_meter(r.id))
                insts.append(of13.inst_apply([of13.act_output(out)]))
                x = sw.xid()
                sent[i].append(x)
                sw.send(of13.flow_mod(match, insts, priority=prio, cookie=r.id,
                                      hard_timeout=hard, xid=x))
            for sw, xids in zip(sws, sent):                   # confirm: barrier, then look for errors
                if not sw.barrier():
                    raise InstallError("barrier timeout on dpid %d" % sw.dpid)
                bad = [e for e in sw.errors if e[0] in xids]
                if bad:
                    raise InstallError("dpid %d rejected: %s" % (sw.dpid, bad))
        except (OSError, InstallError) as e:
            self.remove(r)                                    # roll back whatever landed
            raise e if isinstance(e, InstallError) else InstallError(str(e))
        log.info("installed rid=%d prio=%d hops=%s", r.id, prio, hops)

    def remove(self, r):
        for i, (dpid, _, _) in enumerate(self.topo.switch_hops(r.path)):   # ingress first
            sw = self.get_switch(dpid)
            if sw is None or not sw.ready:
                log.warning("rid=%d: dpid %d gone, relying on hard_timeout", r.id, dpid)
                continue
            try:
                sw.send(of13.flow_mod(command=of13.OFPFC_DELETE, cookie=r.id, cookie_mask=ALL_COOKIE,
                                      table_id=of13.OFPTT_ALL, xid=sw.xid()))
                if i == 0:
                    sw.send(of13.meter_mod(r.id, command=of13.OFPMC_DELETE, xid=sw.xid()))
            except OSError as e:
                log.warning("rid=%d: remove on dpid %d failed: %s", r.id, dpid, e)
        log.info("removed rid=%d", r.id)
