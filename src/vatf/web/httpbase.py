"""The HTTP server under the dashboard and the real-call webhook: the standard library's threaded server,
with a cap on connections handled at once (VOCTO_MAX_CONNECTIONS, default 256), so a flood of
connections can't start unlimited threads, and a time limit on slow clients (a request that trickles in
byte by byte is dropped after `timeout` seconds of silence)."""

from __future__ import annotations

import os
import socket
import threading
from http.server import ThreadingHTTPServer


def _env_int(name: str, default: int) -> int:
    try:
        return max(4, int(os.environ.get(name, default)))
    except ValueError:
        return default


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, handler, max_connections: int | None = None, bind_and_activate: bool = True):
        self.max_connections = max_connections or _env_int("VOCTO_MAX_CONNECTIONS", 256)
        self._slots = threading.BoundedSemaphore(self.max_connections)
        if ":" in str(address[0]):
            self.address_family = socket.AF_INET6
        super().__init__(address, handler, bind_and_activate)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            # Full: answer at once and close, rather than queue without limit.
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 2\r\nContent-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
