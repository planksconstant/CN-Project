import collections
import itertools
import threading

from . import of13


class Switch:
    """One connected OpenFlow switch (one TCP connection)."""

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.dpid = None
        self.ready = False                       # True once static flows are in
        self._xid = itertools.count(1)
        self._lock = threading.Lock()            # serialise writers
        self.errors = collections.deque(maxlen=256)   # (xid, err_type, err_code) from the switch
        self._barriers = {}                      # xid -> Event

    def xid(self):
        return next(self._xid)

    def send(self, data):
        with self._lock:
            self.sock.sendall(data)

    def recv(self):
        return of13.recv_msg(self.sock)

    def barrier_start(self):
        """Send a barrier request; returns (xid, event) to pass to barrier_wait()."""
        xid = self.xid()
        ev = threading.Event()
        self._barriers[xid] = ev
        self.send(of13.barrier_request(xid))
        return xid, ev

    def barrier_wait(self, xid, ev, timeout=1.0):
        """True once the switch has processed everything sent before the barrier."""
        try:
            return ev.wait(timeout)
        finally:
            self._barriers.pop(xid, None)

    def barrier(self, timeout=1.0):
        """Send a barrier and wait for it. Must NOT be called from this switch's reader thread
        (that thread delivers the reply)."""
        xid, ev = self.barrier_start()
        return self.barrier_wait(xid, ev, timeout)

    def on_barrier_reply(self, xid):
        ev = self._barriers.get(xid)
        if ev:
            ev.set()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __repr__(self):
        return "Switch(dpid=%s)" % (self.dpid,)
