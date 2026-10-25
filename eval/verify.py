#!/usr/bin/env python3
"""
Data-plane verification with real traffic. The controller must already be running:
    terminal 1:  python3 -m controller.main
    terminal 2:  sudo python3 -m eval.verify
Builds the diamond topology, reserves bandwidth through the TCP protocol, runs iperf3 (UDP) and
compares what the receiver actually got with what was promised.
"""
import socket
import subprocess
import sys
import time
from subprocess import PIPE

from mininet.log import setLogLevel
from topo.diamond import build
from controller.client import request
from controller.server import LineReader
from .iperf import received_mbps

CTL = ("127.0.0.1", 9000)
SECS = 6
results = []


def ctl(cmd):
    s = socket.create_connection(CTL)
    try:
        return request(s, LineReader(s), cmd)
    finally:
        s.close()


def start(host, dst_ip, port, mbps):
    return host.popen(["iperf3", "-c", dst_ip, "-p", str(port), "-u", "-b", "%gM" % mbps,
                       "-t", str(SECS), "-J"], stdout=PIPE, stderr=PIPE, universal_newlines=True)


def finish(p):
    out, err = p.communicate(timeout=SECS + 20)
    v = received_mbps(out)
    if v is None:
        print("   iperf3 failed:", (out or err).strip()[:300])
    return v


def check(name, expect, got, tol):
    ok = got is not None and abs(got - expect) <= tol * expect
    results.append(ok)
    print("%-58s expect %5.2f  got %s  %s" %
          (name, expect, "%5.2f" % got if got is not None else "  n/a", "PASS" if ok else "FAIL"))


def main():
    setLogLevel("warning")
    try:
        ctl("STATS")
    except OSError:
        sys.exit("controller not reachable on %s:%d - run: python3 -m controller.main" % CTL)
    net = build()
    net.start()
    time.sleep(3)                                          # let all 4 switches connect
    h1, h2, h3, h4 = [net.get("h%d" % i) for i in (1, 2, 3, 4)]
    h2.cmd("iperf3 -s -p 5201 -D")
    h4.cmd("iperf3 -s -p 5202 -D")
    time.sleep(1)
    print("%-58s %s" % ("scenario (Mbps received)", "result"))
    try:
        r = ctl("RESERVE 10.0.0.1 10.0.0.2 4000 0 300 udp/5201")[0]
        print("reserve 4 Mbps h1->h2 udp/5201:", r)
        rid = r.split()[1]
        check("A  reserved 4 Mbps alone (offered 8)", 4.0, finish(start(h1, "10.0.0.2", 5201, 8)), 0.10)
        check("B  unreserved alone (offered 3, best-effort cap 2)", 2.0,
              finish(start(h3, "10.0.0.4", 5202, 3)), 0.15)
        pa, pb = start(h1, "10.0.0.2", 5201, 8), start(h3, "10.0.0.4", 5202, 3)
        a, b = finish(pa), finish(pb)
        check("C  reserved flow while best-effort competes", 4.0, a, 0.10)
        check("C  best-effort flow while reserved flow runs", 2.0, b, 0.15)
        print("cancel:", ctl("CANCEL " + rid)[0])
        time.sleep(1)
        check("D  same flow after CANCEL (now best-effort)", 2.0, finish(start(h1, "10.0.0.2", 5201, 8)), 0.15)

        r1 = ctl("RESERVE 10.0.0.1 10.0.0.2 6000 0 300 udp/5201")[0]
        r2 = ctl("RESERVE 10.0.0.3 10.0.0.4 6000 0 300 udp/5202")[0]
        print("reserve 6 Mbps h1->h2:", r1, "\nreserve 6 Mbps h3->h4:", r2)
        p1, p2 = r1.split()[2], r2.split()[2]
        results.append(p1 != p2)
        print("%-58s %s" % ("E  second reservation took the other path", "PASS" if p1 != p2 else "FAIL"))
        pa, pb = start(h1, "10.0.0.2", 5201, 8), start(h3, "10.0.0.4", 5202, 8)
        a, b = finish(pa), finish(pb)
        check("E  6 Mbps reservation on path 1", 6.0, a, 0.10)
        check("E  6 Mbps reservation on path 2", 6.0, b, 0.10)
        print("\nmeter counters on s1 (drops show the meter policing the excess):")
        subprocess.call(["ovs-ofctl", "-O", "OpenFlow13", "meter-stats", "s1"])
        print("\ncontroller:", ctl("STATS")[0])
        for l in ctl("UTIL")[1:]:
            if float(l.split()[1]) > 0:
                print("  util", l)
        for i in (1, 2, 3, 4, 5, 6):
            ctl("CANCEL %d" % i)
    finally:
        for h in (h2, h4):
            h.cmd("pkill iperf3")
        net.stop()
    print("\n%d/%d checks passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
