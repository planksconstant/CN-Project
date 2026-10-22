import itertools
import threading

from . import of13


class Switch:
    """One connected OpenFlow switch (one TCP connection)."""

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.dpid = None
        self.ready = False
        self._xid = itertools.count(1)
        self._lock = threading.Lock()   # serialise writers (handler + reservation threads)

    def xid(self):
        return next(self._xid)

    def send(self, data):
        with self._lock:
            self.sock.sendall(data)

    def recv(self):
        return of13.recv_msg(self.sock)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __repr__(self):
        return "Switch(dpid=%s)" % (self.dpid,)
