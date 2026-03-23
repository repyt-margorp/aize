from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import fcntl

from wire.protocol import utc_ts


def lifecycle_path(runtime_root: Path) -> Path:
    return runtime_root / "state" / "processes.json"


def lifecycle_lock_path(runtime_root: Path) -> Path:
    return runtime_root / "state" / "processes.lock"


@contextmanager
def lifecycle_lock(runtime_root: Path):
    lock_path = lifecycle_lock_path(runtime_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def init_lifecycle_state(runtime_root: Path, *, node_id: str, run_id: str) -> dict:
    with lifecycle_lock(runtime_root):
        existing = _load_lifecycle_state_unlocked(runtime_root)
        if existing.get("node_id") == node_id and existing.get("run_id") == run_id:
            state = {
                "node_id": node_id,
                "run_id": run_id,
                "processes": dict(existing.get("processes", {})),
            }
        else:
            state = {
                "node_id": node_id,
                "run_id": run_id,
                "processes": {},
            }
        write_lifecycle_state(runtime_root, state)
        return state


def load_lifecycle_state(runtime_root: Path) -> dict:
    with lifecycle_lock(runtime_root):
        return _load_lifecycle_state_unlocked(runtime_root)


def write_lifecycle_state(runtime_root: Path, state: dict) -> None:
    path = lifecycle_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="processes.", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def register_process(
    runtime_root: Path,
    *,
    process_id: str,
    service_id: str,
    node_id: str,
    status: str,
    reason: str | None = None,
) -> dict:
    with lifecycle_lock(runtime_root):
        state = _load_lifecycle_state_unlocked(runtime_root)
        process = state["processes"].get(process_id, {})
        created_at = process.get("created_at", utc_ts())
        process.update(
            {
                "process_id": process_id,
                "service_id": service_id,
                "node_id": node_id,
                "status": status,
                "reason": reason,
                "created_at": created_at,
                "updated_at": utc_ts(),
            }
        )
        state["processes"][process_id] = process
        write_lifecycle_state(runtime_root, state)
        return state


def get_process_record(runtime_root: Path, process_id: str) -> dict:
    with lifecycle_lock(runtime_root):
        return _load_lifecycle_state_unlocked(runtime_root)["processes"][process_id]


def update_process_fields(
    runtime_root: Path,
    *,
    process_id: str,
    fields: dict,
) -> dict:
    with lifecycle_lock(runtime_root):
        state = _load_lifecycle_state_unlocked(runtime_root)
        process = state["processes"][process_id]
        process.update(fields)
        process["updated_at"] = utc_ts()
        write_lifecycle_state(runtime_root, state)
        return process


def _load_lifecycle_state_unlocked(runtime_root: Path) -> dict:
    path = lifecycle_path(runtime_root)
    if not path.exists():
        return {"processes": {}}
    return json.loads(path.read_text(encoding="utf-8"))
