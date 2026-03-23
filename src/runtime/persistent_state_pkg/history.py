from __future__ import annotations

import queue
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from ._core import (
    DEFAULT_PENDING_INPUT_LIMIT,
    normalize_username,
    read_jsonl,
    remove_file_if_exists,
    session_goal_manager_pending_path,
    session_pending_path,
    session_service_pending_path,
    session_timeline_path,
    state_lock,
    write_jsonl,
)

_history_subscribers: dict[str, set[queue.Queue[dict[str, Any]]]] = defaultdict(set)
_history_subscribers_lock = threading.Lock()


def history_subscriber_key(username: str, session_id: str) -> str:
    return f"{normalize_username(username)}::{session_id}"


def register_history_subscriber(
    *,
    username: str,
    session_id: str,
    subscriber: queue.Queue[dict[str, Any]],
) -> None:
    with _history_subscribers_lock:
        _history_subscribers[history_subscriber_key(username, session_id)].add(subscriber)


def unregister_history_subscriber(
    *,
    username: str,
    session_id: str,
    subscriber: queue.Queue[dict[str, Any]],
) -> None:
    with _history_subscribers_lock:
        _history_subscribers[history_subscriber_key(username, session_id)].discard(subscriber)


def _notify_history_subscribers(*, username: str, session_id: str, entry: dict[str, Any]) -> None:
    dead: list[queue.Queue[dict[str, Any]]] = []
    with _history_subscribers_lock:
        subscribers = list(_history_subscribers.get(history_subscriber_key(username, session_id), set()))
    for subscriber in subscribers:
        try:
            subscriber.put_nowait(entry)
        except queue.Full:
            dead.append(subscriber)
        except Exception:
            dead.append(subscriber)
    if dead:
        with _history_subscribers_lock:
            bucket = _history_subscribers.get(history_subscriber_key(username, session_id), set())
            for subscriber in dead:
                bucket.discard(subscriber)


def append_history(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    entry: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        timeline_path = session_timeline_path(runtime_root, username=normalized, session_id=session_id)
        history = read_jsonl(timeline_path)
        history.append(entry)
        if len(history) > limit:
            # Protect user-visible reply/message entries from being evicted by event flooding.
            # Try to trim only event/agent-direction entries first; fall back to tail-trim if needed.
            evictable_indices = [i for i, e in enumerate(history) if e.get("direction") not in ("in", "out")]
            excess = len(history) - limit
            if len(evictable_indices) >= excess:
                to_remove = set(evictable_indices[:excess])
                history = [e for i, e in enumerate(history) if i not in to_remove]
            else:
                history = history[-limit:]
        write_jsonl(timeline_path, history)
    _notify_history_subscribers(username=normalized, session_id=session_id, entry=entry)
    return history


def get_history(runtime_root: Path, *, username: str, session_id: str) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        timeline_path = session_timeline_path(runtime_root, username=normalized, session_id=session_id)
        return read_jsonl(timeline_path)


def service_pending_state_key(service_id: str, username: str, session_id: str) -> str:
    return f"{service_id}::{normalize_username(username)}::{session_id}"


def agent_pending_state_key(agent_id: str, username: str, session_id: str) -> str:
    return f"{agent_id}::{normalize_username(username)}::{session_id}"


def append_pending_input(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    entry: dict[str, Any],
    limit: int = DEFAULT_PENDING_INPUT_LIMIT,
) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        pending_path = session_pending_path(runtime_root, username=normalized, session_id=session_id)
        pending = read_jsonl(pending_path)
        pending.append(dict(entry))
        if len(pending) > limit:
            pending = pending[-limit:]
        write_jsonl(pending_path, pending)
        return pending


def append_service_pending_input(
    runtime_root: Path,
    *,
    service_id: str,
    agent_id: str | None = None,
    username: str,
    session_id: str,
    entry: dict[str, Any],
    limit: int = DEFAULT_PENDING_INPUT_LIMIT,
) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        queue_agent_id = str(agent_id or service_id).strip() or str(service_id).strip()
        pending_path = session_service_pending_path(
            runtime_root,
            username=normalized,
            session_id=session_id,
            service_id=queue_agent_id,
        )
        pending = read_jsonl(pending_path)
        pending.append(dict(entry))
        if len(pending) > limit:
            pending = pending[-limit:]
        write_jsonl(pending_path, pending)
        return pending


def load_pending_inputs(runtime_root: Path, *, username: str, session_id: str) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        pending_path = session_pending_path(runtime_root, username=normalized, session_id=session_id)
        return read_jsonl(pending_path)


def load_service_pending_inputs(
    runtime_root: Path,
    *,
    service_id: str,
    agent_id: str | None = None,
    username: str,
    session_id: str,
) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        queue_agent_id = str(agent_id or service_id).strip() or str(service_id).strip()
        pending_path = session_service_pending_path(
            runtime_root,
            username=normalized,
            session_id=session_id,
            service_id=queue_agent_id,
        )
        return read_jsonl(pending_path)


def clear_pending_inputs(runtime_root: Path, *, username: str, session_id: str) -> None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        remove_file_if_exists(session_pending_path(runtime_root, username=normalized, session_id=session_id))


def drain_pending_inputs(runtime_root: Path, *, username: str, session_id: str) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        pending_path = session_pending_path(runtime_root, username=normalized, session_id=session_id)
        drained = read_jsonl(pending_path)
        if drained:
            remove_file_if_exists(pending_path)
            return drained
        return []


def drain_service_pending_inputs(
    runtime_root: Path,
    *,
    service_id: str,
    agent_id: str | None = None,
    username: str,
    session_id: str,
) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        queue_agent_id = str(agent_id or service_id).strip() or str(service_id).strip()
        pending_path = session_service_pending_path(
            runtime_root,
            username=normalized,
            session_id=session_id,
            service_id=queue_agent_id,
        )
        drained = read_jsonl(pending_path)
        if drained:
            remove_file_if_exists(pending_path)
        return drained


def append_goal_manager_pending_input(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    entry: dict[str, Any],
    limit: int = DEFAULT_PENDING_INPUT_LIMIT,
) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        pending_path = session_goal_manager_pending_path(runtime_root, username=normalized, session_id=session_id)
        pending = read_jsonl(pending_path)
        pending.append(dict(entry))
        if len(pending) > limit:
            pending = pending[-limit:]
        write_jsonl(pending_path, pending)
        return pending


def load_goal_manager_pending_inputs(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        return read_jsonl(session_goal_manager_pending_path(runtime_root, username=normalized, session_id=session_id))


def drain_goal_manager_pending_inputs(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        pending_path = session_goal_manager_pending_path(runtime_root, username=normalized, session_id=session_id)
        drained = read_jsonl(pending_path)
        if drained:
            remove_file_if_exists(pending_path)
        return drained
