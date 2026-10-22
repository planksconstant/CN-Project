"""
Minimal OpenFlow 1.3 wire protocol, hand-packed with struct. stdlib only.
Everything on the wire is big-endian ("!").
"""
import socket
import struct

OFP_VERSION = 0x04

# --- message types ---
OFPT_HELLO = 0
OFPT_ERROR = 1
OFPT_ECHO_REQUEST = 2
OFPT_ECHO_REPLY = 3
OFPT_FEATURES_REQUEST = 5
OFPT_FEATURES_REPLY = 6
OFPT_PACKET_IN = 10
OFPT_FLOW_REMOVED = 11
OFPT_PACKET_OUT = 13
OFPT_FLOW_MOD = 14
OFPT_MULTIPART_REQUEST = 18
OFPT_MULTIPART_REPLY = 19
OFPT_BARRIER_REQUEST = 20
OFPT_BARRIER_REPLY = 21
OFPT_METER_MOD = 29

# --- reserved values ---
OFP_NO_BUFFER = 0xFFFFFFFF
OFPP_IN_PORT = 0xFFFFFFF8
OFPP_FLOOD = 0xFFFFFFFB
OFPP_ALL = 0xFFFFFFFC
OFPP_CONTROLLER = 0xFFFFFFFD
OFPP_ANY = 0xFFFFFFFF
OFPG_ANY = 0xFFFFFFFF
OFPTT_ALL = 0xFF
OFPCML_NO_BUFFER = 0xFFFF

# --- flow_mod commands / flags ---
OFPFC_ADD = 0
OFPFC_MODIFY = 1
OFPFC_MODIFY_STRICT = 2
OFPFC_DELETE = 3
OFPFC_DELETE_STRICT = 4
OFPFF_SEND_FLOW_REM = 1

# --- meter ---
OFPMC_ADD = 0
OFPMC_MODIFY = 1
OFPMC_DELETE = 2
OFPMF_KBPS = 1
OFPMF_PKTPS = 2
OFPMF_BURST = 4
OFPMF_STATS = 8
OFPMBT_DROP = 1

# --- match / OXM ---
OFPMT_OXM = 1
OFPXMC_OPENFLOW_BASIC = 0x8000
OXM_IN_PORT = 0
OXM_ETH_DST = 3
OXM_ETH_SRC = 4
OXM_ETH_TYPE = 5
OXM_IP_PROTO = 10
OXM_IPV4_SRC = 11
OXM_IPV4_DST = 12
OXM_TCP_SRC = 13
OXM_TCP_DST = 14
OXM_UDP_SRC = 15
OXM_UDP_DST = 16

# --- instructions / actions ---
OFPIT_APPLY_ACTIONS = 4
OFPIT_METER = 6
OFPAT_OUTPUT = 0

HDR = struct.Struct("!BBHI")  # version, type, length, xid
HDR_LEN = HDR.size  # 8


# ---------------------------------------------------------------- framing
def pack_msg(msg_type, body=b"", xid=0):
    return HDR.pack(OFP_VERSION, msg_type, HDR_LEN + len(body), xid) + body


def recv_exact(sock, n):
    """Read exactly n bytes from a TCP stream, or return None on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def recv_msg(sock):
    """Return (version, type, xid, body) or None on EOF."""
    h = recv_exact(sock, HDR_LEN)
    if h is None:
        return None
    ver, mtype, length, xid = HDR.unpack(h)
    body = recv_exact(sock, length - HDR_LEN) if length > HDR_LEN else b""
    if body is None:
        return None
    return ver, mtype, xid, body


# ---------------------------------------------------------------- simple msgs
def hello(xid=0):
    return pack_msg(OFPT_HELLO, b"", xid)


def echo_reply(data=b"", xid=0):
    return pack_msg(OFPT_ECHO_REPLY, data, xid)


def features_request(xid=0):
    return pack_msg(OFPT_FEATURES_REQUEST, b"", xid)


def barrier_request(xid=0):
    return pack_msg(OFPT_BARRIER_REQUEST, b"", xid)


def parse_features_reply(body):
    # datapath_id, n_buffers, n_tables, auxiliary_id, pad[2], capabilities, reserved
    dpid, n_buf, n_tab, aux, caps, _ = struct.unpack("!QIBB2xII", body[:24])
    return {"dpid": dpid, "n_buffers": n_buf, "n_tables": n_tab,
            "aux_id": aux, "capabilities": caps}


def parse_error(body):
    etype, code = struct.unpack("!HH", body[:4])
    return etype, code, body[4:]


# ---------------------------------------------------------------- match
def _oxm(field, value, mask=None):
    hasmask = 1 if mask is not None else 0
    payload = value + (mask or b"")
    return struct.pack("!HBB", OFPXMC_OPENFLOW_BASIC,
                       (field << 1) | hasmask, len(payload)) + payload


def _ipv4(s):
    """'10.0.0.1' -> (value, None); '10.0.0.0/24' -> (value, mask)."""
    if "/" in s:
        addr, plen = s.split("/")
        plen = int(plen)
        mask = (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF if plen else 0
        return socket.inet_aton(addr), struct.pack("!I", mask)
    return socket.inet_aton(s), None


def _mac(s):
    return bytes(int(x, 16) for x in s.split(":"))


def match(in_port=None, eth_src=None, eth_dst=None, eth_type=None,
          ip_proto=None, ipv4_src=None, ipv4_dst=None,
          tcp_src=None, tcp_dst=None, udp_src=None, udp_dst=None):
    """
    Build an ofp_match (OXM). Fields are emitted in a prerequisite-safe order.
    Setting any ipv4_* / ip_proto auto-adds eth_type=0x0800; tcp_*/udp_* auto-add ip_proto.
    Empty call = match everything.
    """
    if (ipv4_src or ipv4_dst or ip_proto is not None or tcp_src is not None
            or tcp_dst is not None or udp_src is not None or udp_dst is not None):
        eth_type = 0x0800 if eth_type is None else eth_type
    if tcp_src is not None or tcp_dst is not None:
        ip_proto = 6
    if udp_src is not None or udp_dst is not None:
        ip_proto = 17

    f = []
    if in_port is not None:
        f.append(_oxm(OXM_IN_PORT, struct.pack("!I", in_port)))
    if eth_dst is not None:
        f.append(_oxm(OXM_ETH_DST, _mac(eth_dst)))
    if eth_src is not None:
        f.append(_oxm(OXM_ETH_SRC, _mac(eth_src)))
    if eth_type is not None:
        f.append(_oxm(OXM_ETH_TYPE, struct.pack("!H", eth_type)))
    if ip_proto is not None:
        f.append(_oxm(OXM_IP_PROTO, struct.pack("!B", ip_proto)))
    if ipv4_src is not None:
        f.append(_oxm(OXM_IPV4_SRC, *_ipv4(ipv4_src)))
    if ipv4_dst is not None:
        f.append(_oxm(OXM_IPV4_DST, *_ipv4(ipv4_dst)))
    for fld, v in ((OXM_TCP_SRC, tcp_src), (OXM_TCP_DST, tcp_dst),
                   (OXM_UDP_SRC, udp_src), (OXM_UDP_DST, udp_dst)):
        if v is not None:
            f.append(_oxm(fld, struct.pack("!H", v)))

    body = b"".join(f)
    length = 4 + len(body)                 # length excludes padding
    pad = (8 - length % 8) % 8             # but struct is padded to 8 bytes
    return struct.pack("!HH", OFPMT_OXM, length) + body + b"\x00" * pad


def parse_match(buf, off=0):
    """Return ({oxm_field: raw_bytes}, bytes_consumed_incl_padding)."""
    _mtype, mlen = struct.unpack_from("!HH", buf, off)
    fields = {}
    p, end = off + 4, off + mlen
    while p < end:
        cls, fm, ln = struct.unpack_from("!HBB", buf, p)
        if cls == OFPXMC_OPENFLOW_BASIC:
            fields[fm >> 1] = buf[p + 4:p + 4 + ln]
        p += 4 + ln
    return fields, (mlen + 7) // 8 * 8


# ---------------------------------------------------------------- actions / instructions
def act_output(port, max_len=OFPCML_NO_BUFFER):
    return struct.pack("!HHIH6x", OFPAT_OUTPUT, 16, port, max_len)


def inst_apply(actions):
    body = b"".join(actions)
    return struct.pack("!HH4x", OFPIT_APPLY_ACTIONS, 8 + len(body)) + body


def inst_meter(meter_id):
    return struct.pack("!HHI", OFPIT_METER, 8, meter_id)


# ---------------------------------------------------------------- flow_mod
def flow_mod(match_=None, instructions=(), command=OFPFC_ADD, priority=0,
             cookie=0, cookie_mask=0, table_id=0, idle_timeout=0,
             hard_timeout=0, buffer_id=OFP_NO_BUFFER, out_port=OFPP_ANY,
             out_group=OFPG_ANY, flags=0, xid=0):
    """
    To delete by reservation id:
        flow_mod(command=OFPFC_DELETE, cookie=rid, cookie_mask=0xFFFFFFFFFFFFFFFF,
                 table_id=OFPTT_ALL)
    """
    if match_ is None:
        match_ = match()
    body = struct.pack("!QQBBHHHIIIH2x", cookie, cookie_mask, table_id, command,
                       idle_timeout, hard_timeout, priority, buffer_id,
                       out_port, out_group, flags)
    body += match_ + b"".join(instructions)
    return pack_msg(OFPT_FLOW_MOD, body, xid)


# ---------------------------------------------------------------- meter_mod (step 4)
def meter_mod(meter_id, rate_kbps=0, burst_kb=0, command=OFPMC_ADD, xid=0):
    """Single drop band, kbps. For DELETE, bands are omitted."""
    flags = OFPMF_KBPS | (OFPMF_BURST if burst_kb else 0)
    body = struct.pack("!HHI", command, flags, meter_id)
    if command != OFPMC_DELETE:
        body += struct.pack("!HHII4x", OFPMBT_DROP, 16, rate_kbps, burst_kb)
    return pack_msg(OFPT_METER_MOD, body, xid)


# ---------------------------------------------------------------- packet_in / packet_out
def parse_packet_in(body):
    buffer_id, total_len, reason, table_id, cookie = struct.unpack_from("!IHBBQ", body, 0)
    fields, used = parse_match(body, 16)
    data = body[16 + used + 2:]            # 2 bytes of pad before the frame
    in_port = struct.unpack("!I", fields[OXM_IN_PORT])[0] if OXM_IN_PORT in fields else None
    return {"buffer_id": buffer_id, "total_len": total_len, "reason": reason,
            "table_id": table_id, "cookie": cookie, "in_port": in_port, "data": data}


def packet_out(actions, data=b"", buffer_id=OFP_NO_BUFFER, in_port=OFPP_CONTROLLER, xid=0):
    acts = b"".join(actions)
    body = struct.pack("!IIH6x", buffer_id, in_port, len(acts)) + acts + data
    return pack_msg(OFPT_PACKET_OUT, body, xid)


# ---------------------------------------------------------------- helpers
def fmt_mac(b):
    return ":".join("%02x" % x for x in b)


def peek_eth(data):
    """(dst, src, ethertype) from a raw frame, for logging."""
    if len(data) < 14:
        return None
    dst, src, et = struct.unpack("!6s6sH", data[:14])
    return fmt_mac(dst), fmt_mac(src), et
