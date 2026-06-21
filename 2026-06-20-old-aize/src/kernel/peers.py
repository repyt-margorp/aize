from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import fcntl

from wire.protocol import utc_ts


def peers_path(runtime_root: Path) -> Path:
    return runtime_root / "state" / "peers.json"


def peers_lock_path(runtime_root: Path) -> Path:
    return runtime_root / "state" / "peers.lock"


@contextmanager
def peers_lock(runtime_root: Path):
    lock_path = peers_lock_path(runtime_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_peers(runtime_root: Path) -> dict[str, dict[str, Any]]:
    with peers_lock(runtime_root):
        path = peers_path(runtime_root)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))


def list_peers(runtime_root: Path) -> list[dict[str, Any]]:
    return list(load_peers(runtime_root).values())


def register_peer(
    runtime_root: Path,
    *,
    node_id: str,
    base_url: str,
    peer_id: str | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    with peers_lock(runtime_root):
        path = peers_path(runtime_root)
        peers = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        record = peers.get(node_id, {})
        peers[node_id] = {
            "node_id": node_id,
            "peer_id": peer_id or record.get("peer_id"),
            "base_url": base_url.rstrip("/"),
            "started_at": started_at or record.get("started_at"),
            "updated_at": utc_ts(),
        }
        _write_peers(path, peers)
        return peers[node_id]


def get_peer(runtime_root: Path, node_id: str) -> dict[str, Any] | None:
    return load_peers(runtime_root).get(node_id)


def _write_peers(path: Path, peers: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="peers.", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(peers, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
