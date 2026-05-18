from __future__ import annotations

import queue
import threading
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from ._core import (
    DEFAULT_PENDING_INPUT_LIMIT,
    normalize_username,
    read_jsonl,
    remove_file_if_exists,
    append_jsonl,
    session_goal_manager_pending_path,
    session_pending_path,
    session_runtime_journal_path,
    session_dir,
    session_service_pending_path,
    session_timeline_path,
    state_lock,
    write_jsonl,
)

_history_subscribers: dict[str, set[queue.Queue[dict[str, Any]]]] = defaultdict(set)
_history_subscribers_lock = threading.Lock()
MAX_HISTORY_STRING_LENGTH = 4000
EXTERNALIZED_TEXT_THRESHOLD_BYTES = 4096


def _sanitize_history_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= MAX_HISTORY_STRING_LENGTH:
            return value
        omitted = len(value) - MAX_HISTORY_STRING_LENGTH
        return f"{value[:MAX_HISTORY_STRING_LENGTH]}...[truncated {omitted} chars]"
    if isinstance(value, list):
        return [_sanitize_history_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_history_value(item) for key, item in value.items()}
    return value


def sanitize_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return _sanitize_history_value(dict(entry))


def runtime_journal_entry(entry: dict[str, Any]) -> dict[str, Any]:
    sanitized = sanitize_history_entry(entry)
    event = sanitized.get("event")
    event_payload = event if isinstance(event, dict) else {}
    event_type = str(sanitized.get("event_type") or event_payload.get("type") or "").strip()
    return {
        "journal_type": "runtime.event",
        "ts": str(sanitized.get("ts") or event_payload.get("ts") or ""),
        "event_type": event_type,
        "entry": sanitized,
    }


def _externalized_text_preview(text: str) -> str:
    if len(text) <= MAX_HISTORY_STRING_LENGTH:
        return text
    omitted = len(text) - MAX_HISTORY_STRING_LENGTH
    return f"{text[:MAX_HISTORY_STRING_LENGTH]}...[externalized {omitted} chars]"


def _externalize_entry_text(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    text = entry.get("text")
    if not isinstance(text, str):
        return dict(entry)
    text_bytes = text.encode("utf-8")
    if len(text_bytes) <= EXTERNALIZED_TEXT_THRESHOLD_BYTES:
        return dict(entry)
    message_id = str(entry.get("message_id") or f"msg-{uuid.uuid4().hex}").strip()
    relative_path = Path("messages") / message_id / "body.txt"
    body_path = session_dir(runtime_root, username=username, session_id=session_id) / relative_path
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text(text, encoding="utf-8")
    stored = dict(entry)
    preview = _externalized_text_preview(text)
    stored["text"] = preview
    stored["text_preview"] = preview
    stored["text_inline"] = False
    stored["text_path"] = str(relative_path)
    stored["size_bytes"] = len(text_bytes)
    stored["message_id"] = message_id
    return stored


def _hydrate_entry_text(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    hydrated = dict(entry)
    if bool(hydrated.get("text_inline", True)):
        return hydrated
    relative_path = str(hydrated.get("text_path") or "").strip()
    if not relative_path:
        return hydrated
    body_path = session_dir(runtime_root, username=username, session_id=session_id) / relative_path
    try:
        hydrated["text"] = body_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return hydrated
    return hydrated


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
    stored_entry = _externalize_entry_text(
        runtime_root,
        username=normalized,
        session_id=session_id,
        entry=entry,
    )
    sanitized_entry = sanitize_history_entry(stored_entry)
    with state_lock(runtime_root):
        timeline_path = session_timeline_path(runtime_root, username=normalized, session_id=session_id)
        append_jsonl(
            session_runtime_journal_path(runtime_root, username=normalized, session_id=session_id),
            runtime_journal_entry(entry),
        )
        history = [sanitize_history_entry(item) for item in read_jsonl(timeline_path)]
        # Skip exact replay of the most recent entry. This avoids local append +
        # HttpBridge event echo writing the same timeline record twice.
        if history and history[-1] == sanitized_entry:
            return history
        history.append(sanitized_entry)
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
    _notify_history_subscribers(username=normalized, session_id=session_id, entry=sanitized_entry)
    return history


def get_history(runtime_root: Path, *, username: str, session_id: str) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        timeline_path = session_timeline_path(runtime_root, username=normalized, session_id=session_id)
        return [
            _hydrate_entry_text(
                runtime_root,
                username=normalized,
                session_id=session_id,
                entry=item,
            )
            for item in read_jsonl(timeline_path)
        ]


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
        pending.append(
            _externalize_entry_text(
                runtime_root,
                username=normalized,
                session_id=session_id,
                entry=entry,
            )
        )
        if len(pending) > limit:
            pending = pending[-limit:]
        write_jsonl(pending_path, pending)
        return [
            _hydrate_entry_text(
                runtime_root,
                username=normalized,
                session_id=session_id,
                entry=item,
            )
            for item in pending
        ]


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
        pending.append(
            _externalize_entry_text(
                runtime_root,
                username=normalized,
                session_id=session_id,
                entry=entry,
            )
        )
        if len(pending) > limit:
            pending = pending[-limit:]
        write_jsonl(pending_path, pending)
        return [
            _hydrate_entry_text(
                runtime_root,
                username=normalized,
                session_id=session_id,
                entry=item,
            )
            for item in pending
        ]


def _service_pending_paths(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    service_id: str,
    agent_id: str | None = None,
) -> list[Path]:
    queue_agent_id = str(agent_id or service_id).strip() or str(service_id).strip()
    primary_path = session_service_pending_path(
        runtime_root,
        username=username,
        session_id=session_id,
        service_id=queue_agent_id,
    )
    paths = [primary_path]
    legacy_service_id = str(service_id).strip()
    if legacy_service_id and queue_agent_id != legacy_service_id:
        paths.append(
            session_service_pending_path(
                runtime_root,
                username=username,
                session_id=session_id,
                service_id=legacy_service_id,
            )
        )
    return paths


def load_pending_inputs(runtime_root: Path, *, username: str, session_id: str) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        pending_path = session_pending_path(runtime_root, username=normalized, session_id=session_id)
        return [
            _hydrate_entry_text(
                runtime_root,
                username=normalized,
                session_id=session_id,
                entry=item,
            )
            for item in read_jsonl(pending_path)
        ]


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
        merged: list[dict[str, Any]] = []
        for pending_path in _service_pending_paths(
            runtime_root,
            username=normalized,
            session_id=session_id,
            service_id=service_id,
            agent_id=agent_id,
        ):
            merged.extend(read_jsonl(pending_path))
        return [
            _hydrate_entry_text(
                runtime_root,
                username=normalized,
                session_id=session_id,
                entry=item,
            )
            for item in merged
        ]


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
            return [
                _hydrate_entry_text(
                    runtime_root,
                    username=normalized,
                    session_id=session_id,
                    entry=item,
                )
                for item in drained
            ]
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
        drained: list[dict[str, Any]] = []
        for pending_path in _service_pending_paths(
            runtime_root,
            username=normalized,
            session_id=session_id,
            service_id=service_id,
            agent_id=agent_id,
        ):
            entries = read_jsonl(pending_path)
            if entries:
                drained.extend(entries)
                remove_file_if_exists(pending_path)
        if drained:
            return [
                _hydrate_entry_text(
                    runtime_root,
                    username=normalized,
                    session_id=session_id,
                    entry=item,
                )
                for item in drained
            ]
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
        pending.append(
            _externalize_entry_text(
                runtime_root,
                username=normalized,
                session_id=session_id,
                entry=entry,
            )
        )
        if len(pending) > limit:
            pending = pending[-limit:]
        write_jsonl(pending_path, pending)
        return [
            _hydrate_entry_text(
                runtime_root,
                username=normalized,
                session_id=session_id,
                entry=item,
            )
            for item in pending
        ]


def load_goal_manager_pending_inputs(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        return [
            _hydrate_entry_text(
                runtime_root,
                username=normalized,
                session_id=session_id,
                entry=item,
            )
            for item in read_jsonl(
                session_goal_manager_pending_path(runtime_root, username=normalized, session_id=session_id)
            )
        ]


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
            return [
                _hydrate_entry_text(
                    runtime_root,
                    username=normalized,
                    session_id=session_id,
                    entry=item,
                )
                for item in drained
            ]
        return drained
