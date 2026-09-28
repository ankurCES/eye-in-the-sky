"""Deny-all logging HTTP(S) proxy for the BYOK egress tests.

Point a client's ``HTTPS_PROXY``/``HTTP_PROXY`` here (with
``NO_PROXY=127.0.0.1,localhost`` so the local stub is reached directly):
every other host the client tries is recorded as ``{method, target}``
(``host:port`` only, never headers or bodies) and refused with 403. Nothing
leaves the machine through it. Adapted from the research's
``settings-design/deny_egress_proxy.py``.
"""
from __future__ import annotations

import socketserver
import threading
import time
from typing import Self

from . import pick_port


class _Handler(socketserver.StreamRequestHandler):
    timeout = 10

    def handle(self) -> None:
        try:
            line = self.rfile.readline(4096).decode("latin-1").strip()
        except OSError:
            return
        parts = line.split()
        method = parts[0] if parts else "?"
        target = parts[1] if len(parts) > 1 else "?"
        if method != "CONNECT" and "://" in target:
            scheme, _, rest = target.partition("://")
            target = f"{scheme}://{rest.split('/', 1)[0]}"
        self.server.record({"t": time.time(), "method": method, "target": target})
        try:
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n"
                             b"Connection: close\r\n\r\n")
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int]):
        super().__init__(addr, _Handler)
        self._lock = threading.Lock()
        self.attempts: list[dict] = []

    def record(self, rec: dict) -> None:
        with self._lock:
            self.attempts.append(rec)


class DenyProxy:
    """``with DenyProxy() as proxy: ... proxy.attempts``"""

    def __init__(self, host: str = "127.0.0.1", port: int | None = None):
        self._server = _Server((host, pick_port(host) if port is None else port))
        self.host, self.port = self._server.server_address[:2]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="deny-proxy", daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def attempts(self) -> list[dict]:
        with self._server._lock:
            return list(self._server.attempts)

    def start(self) -> Self:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
