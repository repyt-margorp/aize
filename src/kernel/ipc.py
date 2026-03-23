"""
UNIX domain socket IPC layer for the Aize kernel.

Replaces the FIFO-based transport (router.control + {service_id}.rx/.tx)
with a single bidirectional UNIX stream socket per connected service.

Protocol:
  1. Service connects to router.sock.
  2. Service sends one handshake line:
       {"type": "ipc.connect", "service_id": "<sender_id>"}
  3. Router registers the connection under that sender_id.
  4. Both sides exchange newline-delimited JSON messages on the same socket.
"""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Iterator

SOCKET_NAME = "router.sock"
HANDSHAKE_TYPE = "ipc.connect"
SYSTEM_SENDERS = frozenset({"user.local", "kernel.local"})


def router_socket_path(runtime_root: Path) -> Path:
    return runtime_root / "ports" / SOCKET_NAME


def create_router_socket(path: Path) -> socket.socket:
    """Create a non-blocking listening UNIX stream socket at *path*."""
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def connect_to_router(runtime_root: Path, sender_id: str) -> "RouterConnection":
    """Connect to router.sock, send handshake, return a RouterConnection."""
    path = router_socket_path(runtime_root)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(path))
    handshake = json.dumps({"type": HANDSHAKE_TYPE, "service_id": sender_id}) + "\n"
    sock.sendall(handshake.encode("utf-8"))
    return RouterConnection(sock)


class RouterConnection:
    """Thread-safe bidirectional connection to the router."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._rfile = sock.makefile("r", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def fileno(self) -> int:
        return self._sock.fileno()

    def __iter__(self) -> Iterator[str]:
        return iter(self._rfile)

    def write(self, data: str) -> None:
        with self._lock:
            self._sock.sendall(data.encode("utf-8"))

    def flush(self) -> None:
        pass  # sendall flushes immediately

    def close(self) -> None:
        try:
            self._rfile.close()
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "RouterConnection":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
