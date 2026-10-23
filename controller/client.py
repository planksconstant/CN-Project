#!/usr/bin/env python3
"""
Reservation client.
  python3 -m controller.client RESERVE 10.0.0.1 10.0.0.2 2000 0 30
  python3 -m controller.client LIST
  python3 -m controller.client            # interactive
"""
import argparse
import socket
import sys

from .server import LineReader

MULTI = {"LIST", "UTIL"}        # replies that end with an END line


def request(sock, rd, line):
    sock.sendall((line + "\n").encode())
    first = rd.readline()
    if first is None:
        raise ConnectionError("server closed connection")
    out = [first]
    if line.split()[0].upper() in MULTI and first.startswith("OK"):
        while True:
            l = rd.readline()
            if l is None or l == "END":
                break
            out.append(l)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("cmd", nargs="*")
    a = ap.parse_args()
    sock = socket.create_connection((a.host, a.port))
    rd = LineReader(sock)
    if a.cmd:
        print("\n".join(request(sock, rd, " ".join(a.cmd))))
        return
    try:
        while True:
            line = input("res> ").strip()
            if not line:
                continue
            print("\n".join(request(sock, rd, line)))
            if line.split()[0].upper() == "QUIT":
                break
    except (EOFError, KeyboardInterrupt):
        print()


if __name__ == "__main__":
    main()
