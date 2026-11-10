"""Loopback HTTP listener with exclusive binding on Windows."""
from __future__ import annotations

import errno
from http.server import ThreadingHTTPServer
import socket
import sys
from typing import cast

from .schema import DataError


class LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    if sys.platform == "win32":
        allow_reuse_address = False
        allow_reuse_port = False

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            super().server_bind()
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE and not (
                sys.platform == "win32" and (
                    exc.errno in {errno.EACCES, 10013, 10048}
                    or getattr(exc, "winerror", None) in {10013, 10048}
                )
            ):
                raise
            port = cast(tuple[str, int], self.server_address)[1]
            raise DataError(
                f"Local port {port} is already in use or unavailable. "
                "Choose another port with --port or use --port 0."
            ) from exc
