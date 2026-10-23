"""TCP front end for the reservation service: thread per client, line-based."""
import logging
import socket
import threading

log = logging.getLogger("resrv")


class LineReader:
    """Buffered line reader over a raw TCP socket (TCP is a byte stream, not messages)."""

    def __init__(self, sock, maxlen=4096):
        self.sock, self.maxlen, self.buf = sock, maxlen, bytearray()

    def readline(self):
        """str without newline; None on EOF; ValueError if the line is too long."""
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                if i > self.maxlen:
                    raise ValueError("line too long")
                line = bytes(self.buf[:i])
                del self.buf[:i + 1]
                return line.decode("utf-8", "replace").strip()
            if len(self.buf) > self.maxlen:
                raise ValueError("line too long")
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self.buf += chunk


class ReservationServer:
    def __init__(self, service, host="127.0.0.1", port=9000, idle_timeout=60):
        self.service = service
        self.idle_timeout = idle_timeout
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, port))
        self.srv.listen(64)
        self.port = self.srv.getsockname()[1]      # useful when port=0 (tests)

    def serve_forever(self):
        while True:
            try:
                conn, addr = self.srv.accept()
            except OSError:
                return                              # closed
            threading.Thread(target=self._client, args=(conn, addr), daemon=True).start()

    def close(self):
        self.srv.close()

    def _client(self, conn, addr):
        conn.settimeout(self.idle_timeout)
        rd = LineReader(conn)
        try:
            while True:
                try:
                    line = rd.readline()
                except ValueError:
                    conn.sendall(b"ERR LINE_TOO_LONG\n")
                    break
                if line is None:
                    break
                if not line:
                    continue
                try:
                    out = self.service.handle(line)
                except Exception:
                    log.exception("handler crashed on %r", line)
                    out = ["ERR INTERNAL"]
                conn.sendall(("\n".join(out) + "\n").encode())
                if line.split()[0].upper() == "QUIT":
                    break
        except OSError:                             # timeout / reset
            pass
        finally:
            conn.close()
