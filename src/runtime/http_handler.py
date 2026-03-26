from __future__ import annotations

import html
import json
import queue
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode
from http.server import BaseHTTPRequestHandler
from typing import Any

from kernel.auth import bootstrap_root_user, create_user, has_users, issue_auth_context, verify_user_password
from kernel.auth import auth_context_allows
from kernel.lifecycle import load_lifecycle_state
from kernel.peers import list_peers, register_peer
from kernel.registry import get_service_record
from runtime.goal_persist import goal_state_response_payload
from runtime.message_builder import (
    maybe_release_session_provider,
    make_dispatch_pending_message,
    make_aize_pending_input,
)
from runtime.persistent_state import (
    append_pending_input,
    clear_session_service_runtime,
    create_child_conversation_session,
    create_session,
    consume_session_due_user_response_wait,
    delete_session,
    get_session_service,
    get_session_settings,
    get_history as get_user_history,
    lease_session_service,
    list_peer_joinable_sessions,
    release_session_service,
    list_all_sessions_with_users,
    list_session_agent_contacts,
    list_sessions,
    load_agent_audit_state,
    normalize_auto_compact_threshold_left_percent,
    normalize_agent_priority,
    register_history_subscriber,
    record_session_agent_contact,
    rename_session,
    resolve_session_context,
    reset_agent_audit_states_for_session,
    select_session,
    session_ui_mode,
    session_operation_allowed,
    unregister_history_subscriber,
    update_session_auto_compact_threshold,
    update_session_goal,
    update_session_goal_flags,
    update_session_user_response_wait,
    update_session_peer_joinable,
    update_session_selected_agents,
    write_agent_file,
    read_agent_file,
    list_agent_files,
    delete_agent_file,
    get_agent_file_dir_acl,
    set_agent_file_dir_acl,
    check_agent_file_acl,
    list_goal_attachments,
    save_goal_attachment,
)
from runtime.session_view import (
    build_worker_count_summary,
    latest_goal_manager_runtime_state,
    maybe_enqueue_mid_turn_progress_inquiry,
    worker_slot_badge,
)
from wire.protocol import (
    message_meta_get,
    utc_ts,
    write_jsonl,
)

DEFAULT_HTTPBRIDGE_RECENT_MESSAGES_LIMIT = 100
MAX_HTTPBRIDGE_RECENT_MESSAGES_LIMIT = 5000
HTTP_EVENT_TEXT_LIMIT = 4000
INITIAL_HTTPBRIDGE_PAGE_HISTORY_LIMIT = 40


def _parse_multipart_bytes(raw: bytes, boundary: str) -> list[dict]:
    """Minimal multipart/form-data parser. Returns list of dicts with name, filename, data."""
    sep = ("--" + boundary).encode("utf-8")
    end = ("--" + boundary + "--").encode("utf-8")
    parts: list[dict] = []
    chunks = raw.split(sep)
    for chunk in chunks[1:]:
        if chunk.lstrip(b"\r\n").startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        header_end = chunk.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        header_bytes = chunk[:header_end]
        body = chunk[header_end + 4:]
        headers: dict[str, str] = {}
        for line in header_bytes.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower().decode("utf-8", errors="replace")] = v.strip().decode("utf-8", errors="replace")
        disposition = headers.get("content-disposition", "")
        name = ""
        filename = ""
        for token in disposition.split(";"):
            token = token.strip()
            if token.startswith("name="):
                name = token[5:].strip().strip('"')
            elif token.startswith("filename="):
                filename = token[9:].strip().strip('"')
        parts.append({"name": name, "filename": filename, "data": body})
    return parts


def _truncate_http_text(value: Any, *, limit: int = HTTP_EVENT_TEXT_LIMIT) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _http_event_summary(event_type: str, event: Any) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    normalized_type = str(event_type or event.get("type") or "").strip()
    if normalized_type == "item.completed":
        item = event.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type") or "").strip()
            if item_type == "agent_message":
                return {
                    "type": normalized_type,
                    "item": {
                        "type": item_type,
                        "text": _truncate_http_text(item.get("text")),
                    },
                }
    if normalized_type == "service.goal_audit_completed":
        payload: dict[str, Any] = {"type": normalized_type}
        if "goal_satisfied" in event:
            payload["goal_satisfied"] = bool(event.get("goal_satisfied"))
        if "goal_feedback" in event:
            payload["goal_feedback"] = _truncate_http_text(event.get("goal_feedback"), limit=1200)
        return payload
    payload = {"type": normalized_type or str(event.get("type") or "").strip()}
    for key in (
        "left_percent",
        "used_percent",
        "compaction",
        "status",
        "goal_satisfied",
        "threshold_left_percent",
        "session_id",
        "service_id",
        "provider",
    ):
        if key in event:
            payload[key] = event.get(key)
    if "text" in event:
        payload["text"] = _truncate_http_text(event.get("text"), limit=1200)
    return payload if len(payload) > 1 or payload.get("type") else None


def _history_entry_for_http(entry: dict[str, Any]) -> dict[str, Any]:
    payload = dict(entry)
    if "text" in payload:
        payload["text"] = _truncate_http_text(payload.get("text"))
    event_type = str(payload.get("event_type") or "")
    event_summary = _http_event_summary(event_type, payload.get("event"))
    if event_summary is None:
        payload.pop("event", None)
    else:
        payload["event"] = event_summary
    return payload


def _entry_service_id(entry: dict[str, Any]) -> str:
    return str(entry.get("service_id") or entry.get("from") or "").strip()


def _extract_goal_event_type(entry: dict[str, Any]) -> str:
    event = entry.get("event")
    if isinstance(event, dict):
        return str(entry.get("event_type") or event.get("type") or "").strip()
    return str(entry.get("event_type") or "").strip()


def _is_goal_manager_entry(entry: dict[str, Any]) -> bool:
    event_type = _extract_goal_event_type(entry)
    return (
        event_type.startswith("service.goal_")
        or event_type.startswith("service.goal_manager_compact_")
        or event_type == "service.post_turn_followup_started"
        or event_type == "service.post_turn_followup_failed"
    )


def _is_agent_turn_related_entry(entry: dict[str, Any]) -> bool:
    if not entry or _is_goal_manager_entry(entry) or str(entry.get("direction") or "") == "out":
        return False
    direction = str(entry.get("direction") or "")
    event_type = str(entry.get("event_type") or "")
    if direction in {"in", "session_input"}:
        return bool(_entry_service_id(entry))
    if direction in {"event", "agent"}:
        return bool(_entry_service_id(entry)) or event_type == "usage" or event_type.startswith("service.")
    return False


def _turn_log_label(entry: dict[str, Any]) -> str:
    event_type = str(entry.get("event_type") or "")
    if event_type == "agent.turn_started":
        return "response started"
    if event_type == "thread.started":
        return "thread started"
    if event_type == "turn.started":
        return "turn started"
    if event_type == "turn.completed":
        return "turn completed"
    if event_type.startswith("item."):
        return event_type
    if str(entry.get("direction") or "") == "session_input":
        return str(entry.get("kind") or "session input")
    if str(entry.get("direction") or "") == "in":
        return "assistant reply"
    return event_type or str(entry.get("direction") or "event")


def _plain_block_kind(entry: dict[str, Any]) -> str | None:
    direction = str(entry.get("direction") or "")
    if direction in {"out", "user"}:
        return "user_block"
    if direction == "session_input":
        return "system_block"
    if direction in {"event", "agent"} and not _is_goal_manager_entry(entry) and not _is_agent_turn_related_entry(entry):
        return "system_block"
    return None


def _cluster_entries_for_initial_html(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ascending = sorted(items, key=lambda item: str(item.get("ts") or ""))
    timeline: list[dict[str, Any]] = []
    agent_clusters: dict[str, dict[str, Any]] = {}
    goal_cluster: dict[str, Any] | None = None
    plain_cluster: dict[str, Any] | None = None

    def finalize_cluster(service_id: str) -> None:
        cluster = agent_clusters.pop(service_id, None)
        if cluster:
            timeline.append(cluster)

    def finalize_all_clusters() -> None:
        for service_id in list(agent_clusters.keys()):
            finalize_cluster(service_id)

    def finalize_goal_cluster() -> None:
        nonlocal goal_cluster
        if goal_cluster:
            timeline.append(goal_cluster)
            goal_cluster = None

    def finalize_plain_cluster() -> None:
        nonlocal plain_cluster
        if plain_cluster:
            timeline.append(plain_cluster)
            plain_cluster = None

    for entry in ascending:
        event_type = str(entry.get("event_type") or "")
        service_id = _entry_service_id(entry)
        if _is_goal_manager_entry(entry):
            finalize_plain_cluster()
            finalize_all_clusters()
            if goal_cluster is None:
                goal_cluster = {"kind": "goal_manager_cluster", "entries": []}
            goal_cluster["entries"].append(entry)
            continue
        finalize_goal_cluster()
        plain_kind = _plain_block_kind(entry)
        if plain_kind:
            finalize_all_clusters()
            if plain_cluster and str(plain_cluster.get("kind") or "") != plain_kind:
                finalize_plain_cluster()
            if plain_cluster is None:
                plain_cluster = {"kind": plain_kind, "entries": []}
            plain_cluster["entries"].append(entry)
            continue
        finalize_plain_cluster()
        if not _is_agent_turn_related_entry(entry):
            timeline.append(entry)
            continue
        if event_type == "agent.turn_started":
            existing = agent_clusters.get(service_id)
            is_empty_turn_started = (
                existing
                and not existing.get("reply_entry")
                and not existing.get("completed")
                and len(existing.get("entries", [])) == 1
                and str(existing["entries"][0].get("event_type") or "") == "agent.turn_started"
            )
            if is_empty_turn_started:
                existing["entries"] = [entry]
            else:
                if existing:
                    finalize_cluster(service_id)
                agent_clusters[service_id] = {
                    "kind": "turn_cluster",
                    "service_id": service_id,
                    "entries": [entry],
                    "reply_entry": None,
                    "completed": False,
                }
            continue
        cluster = agent_clusters.get(service_id)
        if not cluster:
            if service_id and str(entry.get("direction") or "") in {"in", "session_input", "event", "agent"}:
                cluster = {
                    "kind": "turn_cluster",
                    "service_id": service_id,
                    "entries": [],
                    "reply_entry": None,
                    "completed": False,
                }
                agent_clusters[service_id] = cluster
            else:
                timeline.append(entry)
                continue
        elif cluster.get("completed"):
            finalize_cluster(service_id)
            if service_id and str(entry.get("direction") or "") in {"in", "session_input", "event", "agent"}:
                cluster = {
                    "kind": "turn_cluster",
                    "service_id": service_id,
                    "entries": [],
                    "reply_entry": None,
                    "completed": False,
                }
                agent_clusters[service_id] = cluster
            else:
                timeline.append(entry)
                continue
        cluster["entries"].append(entry)
        if event_type == "turn.completed":
            cluster["completed"] = True
            finalize_cluster(service_id)
        elif str(entry.get("direction") or "") == "in":
            cluster["reply_entry"] = entry

    finalize_all_clusters()
    finalize_goal_cluster()
    finalize_plain_cluster()
    return list(reversed(timeline))


def _render_cluster_html(item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or "")
    entries = list(item.get("entries") or [])
    if kind not in {"turn_cluster", "goal_manager_cluster", "user_block", "system_block"} or not entries:
        return ""
    if kind in {"user_block", "system_block"}:
        last = entries[-1]
        title = "User Prompt" if kind == "user_block" else "System"
        badge = "User" if kind == "user_block" else "FIFO / Event"
        latest_text = html.escape(str(last.get("text") or ""))
        log_rows = []
        for event_entry in entries:
            log_rows.append(
                "".join(
                    [
                        "<div class='turn-cluster-log-entry'>",
                        "<div class='turn-cluster-log-entry-head'>",
                        f"<span>{html.escape(_turn_log_label(event_entry))}</span>",
                        f"<span>{html.escape(str(event_entry.get('ts') or ''))}</span>",
                        "</div>",
                        f"<div class='turn-cluster-log-entry-text'>{html.escape(str(event_entry.get('text') or ''))}</div>",
                        (
                            "".join(
                                [
                                    "<details class='turn-cluster-json'>",
                                    "<summary>Raw JSON</summary>",
                                    f"<pre class='event-json'>{html.escape(json.dumps(event_entry.get('event'), ensure_ascii=False, indent=2))}</pre>",
                                    "</details>",
                                ]
                            )
                            if isinstance(event_entry.get("event"), dict)
                            else ""
                        ),
                        "</div>",
                    ]
                )
            )
        return "".join(
            [
                f"<li class='entry {'entry-user-cluster' if kind == 'user_block' else 'entry-system-cluster'}'>",
                "<div class='bubble'>",
                "<div class='turn-cluster-head'>",
                "<div>",
                f"<div class='turn-cluster-title'>{html.escape(title)}</div>",
                f"<div class='turn-cluster-meta'>{'Queued or submitted user input for this session' if kind == 'user_block' else 'System-generated session input and lifecycle events'}</div>",
                "</div>",
                "<div class='turn-cluster-badges'>",
                f"<span class='goal-badge'>{html.escape(badge)}</span>",
                f"<span class='goal-badge'>{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}</span>",
                "</div>",
                "</div>",
                f"<div class='turn-cluster-reply'>{latest_text}</div>",
                "<details class='turn-cluster-events'>",
                "<summary class='turn-cluster-events-head turn-cluster-events-toggle'>",
                "<span>Block Log</span>",
                f"<span>{len(entries)} entries</span>",
                "</summary>",
                "<div class='turn-cluster-log'>",
                "".join(log_rows),
                "</div>",
                "</details>",
                "</div>",
                "</li>",
            ]
        )
    is_goal_cluster = kind == "goal_manager_cluster"
    service_id = str(item.get("service_id") or "")
    title = "GoalManager Review" if is_goal_cluster else ("Claude Code" if "claude" in service_id else "Codex")
    if is_goal_cluster:
        last = entries[-1]
        last_type = str(last.get("event_type") or "")
        if last_type in {"service.goal_audit_failed", "service.post_turn_followup_failed", "service.goal_manager_compact_failed"}:
            progress_text = "Failed"
            progress_class = " is-signal-red"
            meta_text = "GoalManager review hit an error"
        elif last_type == "service.goal_audit_completed":
            progress_text = "Completed"
            progress_class = " is-complete"
            meta_text = "GoalManager finished this review cycle"
        else:
            progress_text = "Reviewing"
            progress_class = " is-signal-blue"
            meta_text = "GoalManager is actively reviewing this session"
        reply_text = html.escape(str(last.get("text") or ""))
    else:
        completed = bool(item.get("completed"))
        progress_text = "TurnCompleted" if completed else "In Progress"
        progress_class = " is-complete" if completed else ""
        meta_text = (
            "TurnCompleted received for this response"
            if completed
            else "Agent is responding and streaming events here"
        )
        reply_entry = item.get("reply_entry") if isinstance(item.get("reply_entry"), dict) else None
        reply_text = html.escape(
            str((reply_entry or {}).get("text") or "Waiting for the final assistant_text / TurnCompleted payload.")
        )
    log_rows = []
    for event_entry in entries:
        if str(event_entry.get("direction") or "") == "session_input":
            continue
        log_rows.append(
            "".join(
                [
                    f"<div class='turn-cluster-log-entry{' is-reply' if str(event_entry.get('direction') or '') == 'in' else ''}'>",
                    "<div class='turn-cluster-log-entry-head'>",
                    f"<span>{html.escape(_turn_log_label(event_entry))}</span>",
                    f"<span>{html.escape(str(event_entry.get('ts') or ''))}</span>",
                    "</div>",
                    f"<div class='turn-cluster-log-entry-text'>{html.escape(str(event_entry.get('text') or ''))}</div>",
                    "</div>",
                ]
            )
        )
    return "".join(
        [
            f"<li class='entry {'entry-goal-cluster' if is_goal_cluster else 'entry-turn-cluster'}'>",
            "<div class='bubble'>",
            "<div class='turn-cluster-head'>",
            "<div>",
            f"<div class='turn-cluster-title'>{html.escape(title)}</div>",
            f"<div class='turn-cluster-meta'>{html.escape(meta_text)}</div>",
            "</div>",
            "<div class='turn-cluster-badges'>",
            f"<span class='goal-badge'>{'GoalManager' if is_goal_cluster else html.escape(title)}</span>",
            f"<span class='goal-badge{progress_class}'>{html.escape(progress_text)}</span>",
            (
                ""
                if is_goal_cluster
                else (
                    "<button"
                    " type='button'"
                    " class='toolbar-button ghost'"
                    " data-agent-controls-button='1'"
                    f" data-agent-service-id='{html.escape(service_id, quote=True)}'"
                    " onclick=\"(function(btn){var sid=String(btn.getAttribute('data-agent-service-id')||'').trim();"
                    "if(window.setAgentPopoverOpen){window.setAgentPopoverOpen(true,sid);return false;}"
                    "var p=document.getElementById('agent-popover');if(p){p.classList.remove('is-hidden');}"
                    "return false;})(this)\""
                    ">Agent</button>"
                )
            ),
            "</div>",
            "</div>",
            f"<div class='turn-cluster-reply{' is-pending' if (not is_goal_cluster and not item.get('reply_entry')) else ''}'>{reply_text}</div>",
            "<details class='turn-cluster-events'>",
            "<summary class='turn-cluster-events-head turn-cluster-events-toggle'>",
            "<span>Event Log</span>",
            f"<span>{len(entries)} entries</span>",
            "</summary>",
            "<div class='turn-cluster-log'>",
            "".join(log_rows),
            "</div>",
            "</details>",
            "</div>",
            "</li>",
        ]
    )


def _render_initial_history_html(items: list[dict[str, Any]], render_entry_html) -> str:
    html_parts: list[str] = []
    for item in _cluster_entries_for_initial_html(items):
        if isinstance(item, dict) and str(item.get("kind") or "") in {"turn_cluster", "goal_manager_cluster", "user_block", "system_block"}:
            html_parts.append(_render_cluster_html(item))
        else:
            html_parts.append(render_entry_html(item))
    return "".join(html_parts)


def _latest_exchange_summaries(items: list[dict[str, Any]]) -> tuple[str, str]:
    latest_user_prompt = ""
    latest_agent_reply = ""
    for entry in items:
        direction = str(entry.get("direction") or "")
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        if not latest_user_prompt and direction in {"out", "user"}:
            latest_user_prompt = text
        if not latest_agent_reply and direction == "in":
            latest_agent_reply = text
        if latest_user_prompt and latest_agent_reply:
            break
    return latest_user_prompt, latest_agent_reply


def _history_tail_with_latest_goal_cluster(
    history: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0 or len(history) <= limit:
        return history
    tail = list(history[-limit:])
    tail_ids = {id(entry) for entry in tail}
    latest_goal_cluster: list[dict[str, Any]] = []
    cluster_started = False
    for entry in reversed(history):
        if _is_goal_manager_entry(entry):
            latest_goal_cluster.append(entry)
            cluster_started = True
            continue
        if cluster_started:
            break
    if not latest_goal_cluster:
        return tail
    latest_goal_cluster.reverse()
    merged = [entry for entry in latest_goal_cluster if id(entry) not in tail_ids]
    merged.extend(tail)
    return merged


def make_handler(
    *,
    # State variables
    runtime_root, manifest, self_service, process_id, log_path,
    default_target, default_provider, history_limit,
    tls_enabled,
    codex_service_pool, claude_service_pool, llm_service_kinds,
    pending, awaiting_replies,
    subscribers, subscribers_lock, stopped,
    _active_goal_audits, _active_goal_audits_lock,
    _active_agent_turns, _active_agent_turns_lock,
    # Nested functions from run_http_service
    release_stale_session_bindings, subscriber_key, append_history,
    send_router_control, enqueue_service_control,
    service_snapshots, session_runtime_payload, peer_descriptor,
    resolve_session_service_for_dispatch, codex_service_candidates_for_session,
    current_llm_service_topology,
    resolve_bound_codex_session, enqueue_goal_dispatch,
    session_auto_compact_threshold,
    context_status_from_entry, latest_context_status,
    stored_context_status, refresh_context_status, ensure_context_status,
    manual_compact_current_session,
    render_entry_html, cookie_value, request_parts,
    requested_session_id, request_positive_int, current_context,
):
    # TTL cache for the expensive "all sessions overview" computation.
    # Both GET / (SessionMap initial state) and GET /overview share this cache.
    _ov_cache_state: list = [None, 0.0, ""]  # [payload | None, monotonic timestamp, cache_key]
    _ov_cache_lock = threading.Lock()
    _OV_CACHE_TTL = 3.5  # seconds

    def _scope_include_all(*, context: dict[str, Any] | None, query: dict[str, list[str]] | None = None) -> bool:
        if not isinstance(context, dict) or not bool(context.get("is_superuser")):
            return False
        raw_values = (query or {}).get("scope") or []
        raw_value = str(raw_values[0] if raw_values else "").strip().lower()
        return raw_value == "all"

    def _visible_session_records(*, viewer_username: str, include_all: bool) -> list[dict[str, Any]]:
        if include_all:
            return list_all_sessions_with_users(runtime_root)
        records: list[dict[str, Any]] = []
        for talk in list_sessions(runtime_root, username=viewer_username):
            entry = dict(talk)
            entry["username"] = viewer_username
            records.append(entry)
        return records

    def _compute_overview_payload(*, viewer_username: str, include_all: bool) -> dict:  # type: ignore[misc]
        release_stale_session_bindings()
        all_sessions = _visible_session_records(viewer_username=viewer_username, include_all=include_all)
        with _active_agent_turns_lock:
            _active_turns_snap = dict(_active_agent_turns)
        with _active_goal_audits_lock:
            _active_audits_snap = dict(_active_goal_audits)
        _snaps = service_snapshots()
        _summaries: list[dict[str, Any]] = []
        for _talk in all_sessions:
            _t_user = str(_talk.get("username", ""))
            _t_id = str(_talk.get("session_id", ""))
            _ov_key = f"{_t_user}::{_t_id}"
            _bound_svc = str(_talk.get("service_id") or "").strip()
            _agent_turn = _active_turns_snap.get(_ov_key)
            _goal_audit = _active_audits_snap.get(_ov_key)
            _active_svc = str((_agent_turn or {}).get("service_id") or "").strip()
            _gm_svc = str((_goal_audit or {}).get("service_id") or _bound_svc).strip()
            _worker = worker_slot_badge(_active_svc or _bound_svc, codex_service_pool=codex_service_pool, claude_service_pool=claude_service_pool)
            _gm_worker = worker_slot_badge(_gm_svc, codex_service_pool=codex_service_pool, claude_service_pool=claude_service_pool) if _goal_audit else None
            _preferred_provider = str(_talk.get("preferred_provider", default_provider)).strip().lower() or default_provider
            _goal_completed = bool(_talk.get("goal_completed", False))
            _goal_progress_state = str(_talk.get("goal_progress_state", "complete" if _goal_completed else "in_progress")).strip().lower()
            _wait_started_at = str(_talk.get("user_response_wait_started_at", "") or "").strip()
            _wait_prompt_text = str(_talk.get("user_response_wait_prompt_text", "") or "").strip()
            _wait_status = (
                "waiting"
                if bool(_talk.get("user_response_wait_active", False))
                else (
                    "timed_out"
                    if str(_talk.get("user_response_wait_last_timeout_at", "") or "").strip()
                    else ("recorded" if _wait_started_at else "idle")
                )
            )
            _summaries.append({
                "username": _t_user,
                "session_id": _t_id,
                "label": str(_talk.get("label", _t_id)),
                "goal_text": str(_talk.get("goal_text", "")).strip(),
                "goal_active": bool(_talk.get("goal_active", False)),
                "goal_completed": _goal_completed,
                "goal_progress_state": _goal_progress_state,
                "preferred_provider": _preferred_provider,
                "bound_service_id": _bound_svc,
                "worker": _worker,
                "agent_running": _agent_turn is not None,
                "goal_manager_state": "running" if _goal_audit else "idle",
                "goal_manager_worker": _gm_worker,
                "auto_resume_enabled": bool(_talk.get("auto_resume_enabled", False)),
                "user_response_wait_status": _wait_status,
                "user_response_wait_active": bool(_talk.get("user_response_wait_active", False)),
                "user_response_wait_started_at": _wait_started_at,
                "user_response_wait_prompt_text": _wait_prompt_text,
                "parent_session_id": str(_talk.get("parent_session_id") or "").strip(),
                "created_by_username": str(_talk.get("created_by_username") or "").strip(),
                "created_by_type": str(_talk.get("created_by_type") or "").strip(),
                "origin_session_id": str(_talk.get("origin_session_id") or "").strip(),
                "origin_goal_id": str(_talk.get("origin_goal_id") or "").strip(),
                "session_ui_mode": session_ui_mode(_talk),
            })
        _wc = build_worker_count_summary(service_snapshots=_snaps, session_summaries=_summaries)
        return {
            "session_summaries": _summaries,
            "worker_counts": _wc,
            "codex_pool": codex_service_pool,
            "claude_pool": claude_service_pool,
            "ts": utc_ts(),
        }

    def _get_overview_cached(*, viewer_username: str, include_all: bool) -> dict:  # type: ignore[misc]
        _now = time.monotonic()
        _cache_key = f"{viewer_username}::{'all' if include_all else 'owned'}"
        with _ov_cache_lock:
            _cached, _ts, _stored_key = _ov_cache_state
            if _cached is not None and _stored_key == _cache_key and (_now - _ts) < _OV_CACHE_TTL:
                return _cached
        _result = _compute_overview_payload(viewer_username=viewer_username, include_all=include_all)
        with _ov_cache_lock:
            _ov_cache_state[0] = _result
            _ov_cache_state[1] = time.monotonic()
            _ov_cache_state[2] = _cache_key
        return _result

    def _goal_manager_runtime_payload(
        *,
        username: str,
        session_id: str,
        bound_service_id: str | None = None,
        history_entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        scope_key = subscriber_key(username, session_id)
        with _active_goal_audits_lock:
            active_audit = dict(_active_goal_audits.get(scope_key) or {})
        if active_audit:
            service_id = str(active_audit.get("service_id") or bound_service_id or "").strip()
            return {
                "goal_manager_state": "running",
                "goal_manager_service_id": service_id,
                "goal_manager_worker": worker_slot_badge(
                    service_id,
                    codex_service_pool=codex_service_pool,
                    claude_service_pool=claude_service_pool,
                ) if service_id else None,
            }
        if history_entries is None:
            history_entries = get_user_history(
                runtime_root,
                username=username,
                session_id=session_id,
            )
        runtime_state = latest_goal_manager_runtime_state(history_entries)
        service_id = str(runtime_state.get("service_id") or bound_service_id or "").strip()
        return {
            "goal_manager_state": str(runtime_state.get("state") or "idle"),
            "goal_manager_service_id": service_id,
            "goal_manager_worker": worker_slot_badge(
                service_id,
                codex_service_pool=codex_service_pool,
                claude_service_pool=claude_service_pool,
                ) if service_id else None,
        }

    def _initial_session_summaries_for_view(*, viewer_username: str, include_all: bool) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for talk in _visible_session_records(viewer_username=viewer_username, include_all=include_all):
            username = str(talk.get("username") or viewer_username).strip() or viewer_username
            session_id = str(talk.get("session_id") or "").strip()
            if not session_id:
                continue
            bound_service_id = str(talk.get("service_id") or "").strip()
            wait_started_at = str(talk.get("user_response_wait_started_at", "") or "").strip()
            wait_status = (
                "waiting"
                if bool(talk.get("user_response_wait_active", False))
                else (
                    "timed_out"
                    if str(talk.get("user_response_wait_last_timeout_at", "") or "").strip()
                    else ("recorded" if wait_started_at else "idle")
                )
            )
            goal_manager_runtime = _goal_manager_runtime_payload(
                username=username,
                session_id=session_id,
                bound_service_id=bound_service_id or None,
            )
            summaries.append(
                {
                    "username": username,
                    "session_id": session_id,
                    "label": str(talk.get("label", session_id)),
                    "goal_text": str(talk.get("goal_text", "")).strip(),
                    "goal_active": bool(talk.get("goal_active", False)),
                    "goal_completed": bool(talk.get("goal_completed", False)),
                    "goal_progress_state": str(
                        talk.get(
                            "goal_progress_state",
                            "complete" if bool(talk.get("goal_completed", False)) else "in_progress",
                        )
                    ).strip().lower(),
                    "preferred_provider": str(talk.get("preferred_provider", default_provider)).strip().lower() or default_provider,
                    "bound_service_id": bound_service_id,
                    "worker": worker_slot_badge(
                        bound_service_id,
                        codex_service_pool=codex_service_pool,
                        claude_service_pool=claude_service_pool,
                    ) if bound_service_id else None,
                    "agent_running": False,
                    "goal_manager_state": str(goal_manager_runtime.get("goal_manager_state") or "idle"),
                    "goal_manager_worker": goal_manager_runtime.get("goal_manager_worker"),
                    "auto_resume_enabled": bool(talk.get("auto_resume_enabled", False)),
                    "user_response_wait_status": wait_status,
                    "user_response_wait_active": bool(talk.get("user_response_wait_active", False)),
                    "user_response_wait_started_at": wait_started_at,
                    "parent_session_id": str(talk.get("parent_session_id") or "").strip(),
                    "created_by_username": str(talk.get("created_by_username") or "").strip(),
                    "created_by_type": str(talk.get("created_by_type") or "").strip(),
                    "origin_session_id": str(talk.get("origin_session_id") or "").strip(),
                    "origin_goal_id": str(talk.get("origin_goal_id") or "").strip(),
                    "session_ui_mode": session_ui_mode(talk),
                }
            )
        return summaries

    def _render_session_nav_items(
        *,
        session_summaries: list[dict[str, Any]],
        active_session_id: str,
        session_scope: str,
    ) -> str:
        parts: list[str] = []
        scope_suffix = "&scope=all" if session_scope == "all" else ""
        for summary in session_summaries:
            sid = str(summary.get("session_id") or "").strip()
            if not sid:
                continue
            label = str(summary.get("label") or sid)
            active = sid == active_session_id
            goal_active = bool(summary.get("goal_active"))
            goal_completed = bool(summary.get("goal_completed"))
            wait_status = str(summary.get("user_response_wait_status") or "idle").strip()
            wait_active = bool(summary.get("user_response_wait_active", False))
            created_by_username = str(summary.get("created_by_username") or "").strip() or "unknown"
            origin_session_id = str(summary.get("origin_session_id") or "").strip()
            origin_meta = (
                f"from {created_by_username} via {origin_session_id}"
                if origin_session_id
                else f"created by {created_by_username}"
            )
            parts.append(
                "".join(
                    [
                        f"<a class='talk-nav-item{' is-active' if active else ''}' href='/?session_id={html.escape(sid)}{scope_suffix}'>",
                        "<span class='talk-nav-head'>",
                        f"<span class='talk-nav-title'>{html.escape(label)}</span>",
                        "<span class='talk-nav-signals'>",
                        f"<span class='talk-signal talk-signal-active{' is-on' if goal_active else ''}' title='Goal active state'>●</span>",
                        f"<span class='talk-signal talk-signal-completed{' is-on' if goal_completed else ''}' title='Goal completed state'>●</span>",
                        f"<span class='talk-signal talk-signal-wait{' is-on' if wait_status != 'idle' else ''}{' is-waiting' if wait_active else ''}{' is-timeout' if wait_status == 'timed_out' else ''}' title='User response wait state'>●</span>",
                        "</span>",
                        "</span>",
                        f"<span class='talk-nav-meta'>{html.escape(sid)}</span>",
                        f"<span class='talk-nav-origin'>{html.escape(origin_meta)}</span>",
                        "</a>",
                    ]
                )
            )
        return "".join(parts)

    def _render_goal_board_items(
        *,
        session_summaries: list[dict[str, Any]],
        active_session_id: str,
        session_scope: str,
    ) -> str:
        parts: list[str] = []
        scope_suffix = "&scope=all" if session_scope == "all" else ""
        for summary in session_summaries:
            sid = str(summary.get("session_id") or "").strip()
            if not sid:
                continue
            label = str(summary.get("label") or sid)
            goal_text = str(summary.get("goal_text") or "").strip()
            goal_active = bool(summary.get("goal_active"))
            goal_completed = bool(summary.get("goal_completed"))
            goal_manager_state = str(summary.get("goal_manager_state") or "idle")
            wait_status = str(summary.get("user_response_wait_status") or "idle").strip()
            wait_active = bool(summary.get("user_response_wait_active", False))
            created_by_username = str(summary.get("created_by_username") or "").strip() or "unknown"
            origin_session_id = str(summary.get("origin_session_id") or "").strip()
            origin_meta = (
                f" | from {created_by_username} via {origin_session_id}"
                if origin_session_id
                else f" | created by {created_by_username}"
            )
            classes = ["goal-session-card"]
            if sid == active_session_id:
                classes.append("is-active-talk")
            if goal_manager_state == "running":
                classes.append("is-goal-running")
            if not goal_active:
                classes.append("is-goal-inactive")
            worker = summary.get("worker") if isinstance(summary.get("worker"), dict) else None
            goal_worker = summary.get("goal_manager_worker") if isinstance(summary.get("goal_manager_worker"), dict) else None
            worker_provider = str((worker or {}).get("provider") or summary.get("preferred_provider") or "codex")
            worker_slot = "·" if (worker or {}).get("slot") is None else str((worker or {}).get("slot"))
            gm_slot = "·" if (goal_worker or {}).get("slot") is None else str((goal_worker or {}).get("slot"))
            goal_html = html.escape(goal_text) if goal_text else "<span class='goal-session-empty'>No goal</span>"
            parts.append(
                "".join(
                    [
                        f"<a class='{' '.join(classes)}' href='/?session_id={html.escape(sid)}{scope_suffix}' title='Open this session'>",
                        f"<span class='goal-marker goal-marker-left{' is-claude' if worker_provider == 'claude' else ''}{'' if worker else ' is-idle'}' title='Bound/selected worker'>{html.escape(worker_slot)}</span>",
                        f"<span class='goal-marker goal-marker-right{'' if goal_manager_state == 'running' else ' is-hidden'}' title='GoalManager running'>{html.escape(gm_slot)}</span>",
                        "<div class='goal-session-card-head'>",
                        f"<div class='goal-session-title'>{html.escape(label)}</div>",
                        "</div>",
                        f"<div class='goal-session-meta'>{html.escape(summary.get('username', ''))}{' · ' if summary.get('username') else ''}{html.escape(sid)}{html.escape(origin_meta)}</div>",
                        f"<div class='goal-session-goal'>{goal_html}</div>",
                        "<div class='goal-session-state'>",
                        f"<span class='goal-session-badge{' is-on' if goal_active else ''}'>{'Active' if goal_active else 'Inactive'}</span>",
                        f"<span class='goal-session-badge{' is-done' if goal_completed else ''}'>{'Completed' if goal_completed else 'In Progress'}</span>",
                        (
                            f"<span class='goal-session-badge{' is-warn' if wait_active else ''}'>"
                            f"{'Waiting User Response' if wait_active else ('Wait Timed Out' if wait_status == 'timed_out' else 'Wait Recorded')}"
                            "</span>"
                            if wait_status != "idle"
                            else ""
                        ),
                        "</div>",
                        "</a>",
                    ]
                )
            )
        return "".join(parts)

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.1 is required for WebSocket upgrade (101 Switching Protocols)
        protocol_version = "HTTP/1.1"

        def _set_session_cookie(self, token: str | None) -> None:
            parts = ["bridge_session="]
            if token:
                parts[0] = f"bridge_session={token}"
            parts.extend(["Path=/", "HttpOnly", "SameSite=Lax"])
            if tls_enabled:
                parts.append("Secure")
            if token is None:
                parts.extend(["Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT"])
            self.send_header("Set-Cookie", "; ".join(parts))

        def _html(self, status: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json_with_cookie(self, status: int, payload: dict[str, Any], token: str | None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._set_session_cookie(token)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str, *, token: str | None | object = ... ) -> None:
            self.send_response(303)
            if token is not ...:
                self._set_session_cookie(None if token is None else str(token))
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _trace_auth_request(
            self,
            *,
            phase: str,
            path: str,
            username: str = "",
            context: dict[str, Any] | None = None,
        ) -> None:
            cookie_header = self.headers.get("Cookie") or ""
            token_present = bool(cookie_value("bridge_session", cookie_header))
            payload = {
                "type": "http.auth_trace",
                "ts": utc_ts(),
                "service_id": self_service.get("service_id"),
                "process_id": process_id,
                "phase": phase,
                "method": self.command,
                "path": path,
                "host": str(self.headers.get("Host") or ""),
                "origin": str(self.headers.get("Origin") or ""),
                "referer": str(self.headers.get("Referer") or ""),
                "user_agent": str(self.headers.get("User-Agent") or ""),
                "cookie_present": token_present,
                "username": username,
                "context_username": str((context or {}).get("username") or ""),
                "context_session_id": str((context or {}).get("session_id") or ""),
            }
            write_jsonl(log_path, payload)

        def _require_user(
            self,
            *,
            payload: dict[str, Any] | None = None,
            query: dict[str, list[str]] | None = None,
        ) -> dict[str, str] | None:
            context = current_context(self, payload=payload, query=query)
            if context:
                return context
            self._json(401, {"error": "auth_required_or_invalid_talk"})
            return None

        def do_GET(self) -> None:
            path, query = request_parts(self)
            if path == "/ws":
                return self._do_WS_upgrade()
            if path == "/":
                return self._do_GET_root(path, query)
            if path == "/events":
                return self._do_GET_events(path, query)
            if path == "/health":
                return self._do_GET_health(path, query)
            if path == "/peer/ping":
                return self._do_GET_peer_ping(path, query)
            if path == "/federation/peers":
                return self._do_GET_federation_peers(path, query)
            if path == "/session/goal/state":
                return self._do_GET_session_goal_state(path, query)
            if path == "/messages":
                return self._do_GET_messages(path, query)
            if path == "/sessions":
                return self._do_GET_sessions(path, query)
            if path == "/services":
                return self._do_GET_services(path, query)
            if path == "/overview":
                return self._do_GET_overview(path, query)
            if path == "/session/agent-file/list":
                return self._do_GET_agent_file_list(path, query)
            if path == "/session/agent-file/read":
                return self._do_GET_agent_file_read(path, query)
            if path == "/session/agent-file/acl":
                return self._do_GET_agent_file_acl(path, query)
            if path == "/session/goal/attachments":
                return self._do_GET_goal_attachments(path, query)
            if path == "/session/goal/attachment":
                return self._do_GET_goal_attachment(path, query)
            self._json(404, {"error": "not_found"})

        def _do_GET_root(self, path: str, query: dict) -> None:
            from runtime.html_renderer import render_login_page, render_main_page
            context = current_context(self, query=query)
            self._trace_auth_request(phase="get_root", path=path, context=context)
            if not context:
                req_session_id = requested_session_id(self, query=query)
                login_hidden_talk = (
                    f"<input type='hidden' name='session_id' value='{html.escape(req_session_id)}'>"
                    if req_session_id
                    else ""
                )
                bootstrap_needed = not has_users(runtime_root)
                self._html(200, render_login_page(
                    display_name=str(self_service["display_name"]),
                    bootstrap_needed=bootstrap_needed,
                    login_hidden_talk=login_hidden_talk,
                ))
                return
            username = context["username"]
            viewer_username = str(context.get("viewer_username") or username)
            session_id = context["session_id"]
            role_name = context.get("role", "user")
            is_superuser = bool(context.get("is_superuser"))
            initial_session_scope = "all" if _scope_include_all(context=context, query=query) else "owned"
            initial_session_map_open = requested_session_id(self, query=query) is None
            initial_context_status = stored_context_status(username, session_id)
            initial_history = get_user_history(
                runtime_root,
                username=username,
                session_id=session_id,
            )
            initial_history = _history_tail_with_latest_goal_cluster(
                initial_history,
                limit=INITIAL_HTTPBRIDGE_PAGE_HISTORY_LIMIT,
            )
            initial_history_for_http = [_history_entry_for_http(entry) for entry in initial_history]
            entries_json = json.dumps(initial_history_for_http, ensure_ascii=False).replace("</", "<\\/")
            context_status_json = json.dumps(initial_context_status, ensure_ascii=False).replace("</", "<\\/")
            initial_auto_compact_threshold = session_auto_compact_threshold(username, session_id)
            session_settings = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
            initial_session_label = str(session_settings.get("label", session_id))
            initial_goal_text = str(session_settings.get("goal_text", ""))
            initial_active_goal_id = str(session_settings.get("active_goal_id", "") or "")
            initial_goal_history_json = json.dumps(
                list(session_settings.get("goal_history", []))
                if isinstance(session_settings.get("goal_history"), list)
                else [],
                ensure_ascii=False,
            ).replace("</", "<\\/")
            initial_goal_active = bool(session_settings.get("goal_active", bool(initial_goal_text)))
            initial_goal_completed = bool(session_settings.get("goal_completed", False))
            initial_goal_progress_state = str(
                session_settings.get("goal_progress_state", "complete" if initial_goal_completed else "in_progress")
            )
            _bound_service_for_ui = get_session_service(runtime_root, username=username, session_id=session_id)
            initial_goal_audit_state = (
                load_agent_audit_state(
                    runtime_root,
                    service_id=_bound_service_for_ui,
                    username=username,
                    session_id=session_id,
                )
                if _bound_service_for_ui
                else "all_clear"
            )
            initial_goal_reset_completed_on_prompt = bool(
                session_settings.get("goal_reset_completed_on_prompt", True)
            )
            initial_goal_auto_compact_enabled = bool(
                session_settings.get("goal_auto_compact_enabled", True)
            )
            initial_auto_resume_enabled = bool(session_settings.get("auto_resume_enabled", True))
            initial_auto_resume_interval_seconds = int(session_settings.get("auto_resume_interval_seconds", 21600) or 21600)
            initial_auto_resume_next_at = str(session_settings.get("auto_resume_next_at", "") or "")
            initial_auto_resume_reason = str(session_settings.get("auto_resume_reason", "") or "")
            initial_user_response_wait_status = (
                "waiting"
                if bool(session_settings.get("user_response_wait_active", False))
                else (
                    "timed_out"
                    if str(session_settings.get("user_response_wait_last_timeout_at", "") or "").strip()
                    else (
                        "answered"
                        if str(session_settings.get("user_response_wait_last_cleared_at", "") or "").strip()
                        else (
                            "recorded"
                            if str(session_settings.get("user_response_wait_started_at", "") or "").strip()
                            else "idle"
                        )
                    )
                )
            )
            initial_user_response_wait_active = bool(session_settings.get("user_response_wait_active", False))
            initial_user_response_wait_timeout_seconds = int(session_settings.get("user_response_wait_timeout_seconds", 300) or 300)
            initial_user_response_wait_effective_timeout_seconds = int(
                session_settings.get("user_response_wait_effective_timeout_seconds", 300) or 300
            )
            initial_user_response_wait_started_at = str(session_settings.get("user_response_wait_started_at", "") or "")
            initial_user_response_wait_until_at = str(session_settings.get("user_response_wait_until_at", "") or "")
            initial_user_response_wait_prompt_text = str(session_settings.get("user_response_wait_prompt_text", "") or "")
            initial_user_response_wait_last_cleared_at = str(session_settings.get("user_response_wait_last_cleared_at", "") or "")
            initial_user_response_wait_last_timeout_at = str(session_settings.get("user_response_wait_last_timeout_at", "") or "")
            initial_session_group = str(session_settings.get("session_group", "user") or "user")
            initial_session_ui_mode = session_ui_mode(session_settings)
            initial_session_map_open = bool(initial_session_map_open or initial_session_ui_mode == "map_only")
            initial_session_permissions_json = json.dumps(
                dict(session_settings.get("session_permissions", {}))
                if isinstance(session_settings.get("session_permissions"), dict)
                else {},
                ensure_ascii=False,
            ).replace("</", "<\\/")
            initial_preferred_provider = str(session_settings.get("preferred_provider", default_provider))
            initial_agent_priority = normalize_agent_priority(session_settings.get("agent_priority"))
            try:
                initial_session_priority: int = max(0, min(100, int(session_settings.get("session_priority", 50))))
            except (TypeError, ValueError):
                initial_session_priority = 50
            initial_agent_welcome_enabled = bool(session_settings.get("agent_welcome_enabled", False))
            initial_welcomed_agents = list_session_agent_contacts(runtime_root, username=username, session_id=session_id)
            initial_selected_agents = list(session_settings.get("selected_agents", [])) if isinstance(session_settings.get("selected_agents"), list) else []
            initial_goal_manager_state = _goal_manager_runtime_payload(
                username=username,
                session_id=session_id,
                bound_service_id=_bound_service_for_ui,
            ).get("goal_manager_state", "idle")
            # When the SessionMap is open on initial load (no specific session in URL),
            # populate session summaries from the TTL cache so GET / is fast.
            # When a specific session is requested, skip the expensive all-sessions
            # computation entirely — the client will fetch /overview lazily when needed.
            initial_session_summaries = _initial_session_summaries_for_view(
                viewer_username=viewer_username,
                include_all=(initial_session_scope == "all"),
            )
            if initial_session_map_open:
                try:
                    _paged_ov = _get_overview_cached(
                        viewer_username=viewer_username,
                        include_all=(initial_session_scope == "all"),
                    )
                    initial_session_summaries_json = json.dumps(_paged_ov["session_summaries"], ensure_ascii=False).replace("</", "<\\/")
                    initial_worker_counts_json = json.dumps(_paged_ov["worker_counts"], ensure_ascii=False).replace("</", "<\\/")
                except Exception:
                    initial_session_summaries_json = "[]"
                    initial_worker_counts_json = "{}"
            else:
                initial_session_summaries_json = json.dumps(initial_session_summaries, ensure_ascii=False).replace("</", "<\\/")
                initial_worker_counts_json = "{}"
            session_nav_items = _render_session_nav_items(
                session_summaries=initial_session_summaries,
                active_session_id=session_id,
                session_scope=initial_session_scope,
            )
            goal_board_items = _render_goal_board_items(
                session_summaries=initial_session_summaries,
                active_session_id=session_id,
                session_scope=initial_session_scope,
            )
            sidebar_system_html = (
                "".join(
                    [
                        "<div class='stack'>",
                        "<p><strong>Superuser</strong></p>",
                        "<p>Create additional UI users directly from HTTPBridge. Passwords stay in runtime state and are not tracked in Git.</p>",
                        "<button id='account-register-toggle' class='ghost' type='button'>Account Register</button>",
                        "</div>",
                    ]
                )
                if is_superuser
                else ""
            )
            items = _render_initial_history_html(initial_history_for_http, render_entry_html)
            initial_latest_user_prompt, initial_latest_agent_reply = _latest_exchange_summaries(initial_history_for_http)
            self._html(
                200,
                render_main_page(
                    username=username,
                    session_id=session_id,
                    role_name=role_name,
                    is_superuser=is_superuser,
                    initial_session_scope=initial_session_scope,
                    display_name=str(self_service["display_name"]),
                    default_target=default_target,
                    default_provider=default_provider,
                    initial_session_map_open=initial_session_map_open,
                    entries_json=entries_json,
                    context_status_json=context_status_json,
                    initial_auto_compact_threshold=initial_auto_compact_threshold,
                    initial_session_label=initial_session_label,
                    initial_goal_text=initial_goal_text,
                    initial_active_goal_id=initial_active_goal_id,
                    initial_goal_history_json=initial_goal_history_json,
                    initial_goal_active=initial_goal_active,
                    initial_goal_completed=initial_goal_completed,
                    initial_goal_progress_state=initial_goal_progress_state,
                    initial_goal_audit_state=initial_goal_audit_state,
                    initial_bound_service_id=_bound_service_for_ui,
                    default_httpbridge_recent_messages_limit=DEFAULT_HTTPBRIDGE_RECENT_MESSAGES_LIMIT,
                    initial_goal_reset_completed_on_prompt=initial_goal_reset_completed_on_prompt,
                    initial_goal_auto_compact_enabled=initial_goal_auto_compact_enabled,
                    initial_auto_resume_enabled=initial_auto_resume_enabled,
                    initial_auto_resume_interval_seconds=initial_auto_resume_interval_seconds,
                    initial_auto_resume_next_at=initial_auto_resume_next_at,
                    initial_auto_resume_reason=initial_auto_resume_reason,
                    initial_user_response_wait_status=initial_user_response_wait_status,
                    initial_user_response_wait_active=initial_user_response_wait_active,
                    initial_user_response_wait_timeout_seconds=initial_user_response_wait_timeout_seconds,
                    initial_user_response_wait_effective_timeout_seconds=initial_user_response_wait_effective_timeout_seconds,
                    initial_user_response_wait_started_at=initial_user_response_wait_started_at,
                    initial_user_response_wait_until_at=initial_user_response_wait_until_at,
                    initial_user_response_wait_prompt_text=initial_user_response_wait_prompt_text,
                    initial_user_response_wait_last_cleared_at=initial_user_response_wait_last_cleared_at,
                    initial_user_response_wait_last_timeout_at=initial_user_response_wait_last_timeout_at,
                    initial_session_group=initial_session_group,
                    initial_session_ui_mode=initial_session_ui_mode,
                    initial_session_permissions_json=initial_session_permissions_json,
                    initial_preferred_provider=initial_preferred_provider,
                    initial_agent_priority=initial_agent_priority,
                    initial_session_priority=initial_session_priority,
                    initial_goal_manager_state=str(initial_goal_manager_state),
                    initial_agent_welcome_enabled=initial_agent_welcome_enabled,
                    initial_welcomed_agents=initial_welcomed_agents,
                    initial_selected_agents=initial_selected_agents,
                    recent_messages_limit_max=MAX_HTTPBRIDGE_RECENT_MESSAGES_LIMIT,
                    initial_session_summaries_json=initial_session_summaries_json,
                    initial_worker_counts_json=initial_worker_counts_json,
                    initial_latest_user_prompt=initial_latest_user_prompt,
                    initial_latest_agent_reply=initial_latest_agent_reply,
                    session_nav_items=session_nav_items,
                    goal_board_items=goal_board_items,
                    sidebar_system_html=sidebar_system_html,
                    codex_service_pool=codex_service_pool,
                    claude_service_pool=claude_service_pool,
                    items=items,
                )
            )


        def _do_GET_events(self, path: str, query: dict) -> None:
            context = self._require_user(query=query)
            if not context:
                return
            username = context["username"]
            session_id = context["session_id"]
            subscriber: queue.Queue[dict[str, Any]] = queue.Queue()
            register_history_subscriber(username=username, session_id=session_id, subscriber=subscriber)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while not stopped.is_set():
                    try:
                        entry = subscriber.get(timeout=15.0)
                        payload = json.dumps(_history_entry_for_http(entry), ensure_ascii=False)
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                unregister_history_subscriber(username=username, session_id=session_id, subscriber=subscriber)
            return

        def _do_GET_health(self, path: str, query: dict) -> None:
            self._json(
                200,
                {
                    "ok": True,
                    "service_id": self_service["service_id"],
                    "process_id": process_id,
                    "default_target": default_target,
                },
            )
            return

        def _do_GET_peer_ping(self, path: str, query: dict) -> None:
            self._json(200, {"ok": True, "peer": peer_descriptor()})
            return

        def _do_GET_federation_peers(self, path: str, query: dict) -> None:
            self._json(200, {"peers": list_peers(runtime_root)})
            return

        def _do_GET_session_goal_state(self, path: str, query: dict) -> None:
            context = self._require_user(query=query)
            if not context:
                return
            talk = get_session_settings(runtime_root, username=context["username"], session_id=context["session_id"]) or {}
            context_status = talk.get("last_context_status") if isinstance(talk, dict) else None
            context_status_updated_at = (
                talk.get("last_context_status_updated_at") if isinstance(talk, dict) else None
            )
            bound_service_id = get_session_service(runtime_root, username=context["username"], session_id=context["session_id"])
            agent_audit = (
                load_agent_audit_state(
                    runtime_root,
                    service_id=bound_service_id,
                    username=context["username"],
                    session_id=context["session_id"],
                )
                if bound_service_id
                else None
            )
            welcomed = list_session_agent_contacts(runtime_root, username=context["username"], session_id=context["session_id"])
            goal_manager_runtime = _goal_manager_runtime_payload(
                username=context["username"],
                session_id=context["session_id"],
                bound_service_id=bound_service_id,
            )
            self._json(200, {
                **goal_state_response_payload(
                    talk,
                    session_id=context["session_id"],
                    default_provider=default_provider,
                    agent_audit_state=agent_audit,
                    goal_manager_state=str(goal_manager_runtime.get("goal_manager_state") or "idle"),
                    goal_manager_service_id=str(goal_manager_runtime.get("goal_manager_service_id") or ""),
                    goal_manager_worker=goal_manager_runtime.get("goal_manager_worker"),
                    welcomed_agents=welcomed,
                ),
                "label": str(talk.get("label") or context["session_id"]),
                "bound_service_id": bound_service_id or None,
                "auto_compact_threshold_left_percent": normalize_auto_compact_threshold_left_percent(
                    talk.get("auto_compact_threshold_left_percent")
                ),
                "context_status": context_status if isinstance(context_status, dict) else None,
                "context_status_updated_at": str(context_status_updated_at or ""),
            })
            return

        def _do_GET_messages(self, path: str, query: dict) -> None:
            context = self._require_user(query=query)
            if not context:
                return
            limit = request_positive_int(
                query,
                "limit",
                default=DEFAULT_HTTPBRIDGE_RECENT_MESSAGES_LIMIT,
                maximum=MAX_HTTPBRIDGE_RECENT_MESSAGES_LIMIT,
            )
            history = get_user_history(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
            )
            visible_history = _history_tail_with_latest_goal_cluster(history, limit=limit)
            self._json(
                200,
                {
                    "username": context["username"],
                    "session_id": context["session_id"],
                    "session_id": context["session_id"],
                    "messages": [_history_entry_for_http(entry) for entry in visible_history],
                },
            )
            return

        def _do_GET_sessions(self, path: str, query: dict) -> None:
            context = self._require_user(query=query)
            if not context:
                return
            include_all = _scope_include_all(context=context, query=query)
            talks_payload = _compute_overview_payload(
                viewer_username=str(context.get("viewer_username") or context["username"]),
                include_all=include_all,
            )
            self._json(
                200,
                {
                    "username": context["username"],
                    "viewer_username": str(context.get("viewer_username") or context["username"]),
                    "active_session_id": context["session_id"],
                    "scope": "all" if include_all else "owned",
                    "sessions": talks_payload["session_summaries"],
                    "session_summaries": talks_payload["session_summaries"],
                    "worker_counts": talks_payload["worker_counts"],
                },
            )
            return

        def _do_GET_services(self, path: str, query: dict) -> None:
            context = self._require_user(query=query)
            if not context:
                return
            if not auth_context_allows(issue_auth_context(runtime_root, username=context["username"]), "read_service_status"):
                self._json(403, {"error": "read_service_status_required"})
                return
            self._json(
                200,
                {
                    "services": service_snapshots(),
                },
            )
            return

        def _do_GET_overview(self, path: str, query: dict) -> None:
            context = self._require_user(query=query)
            if not context:
                return
            include_all = _scope_include_all(context=context, query=query)
            self._json(
                200,
                {
                    **_get_overview_cached(
                        viewer_username=str(context.get("viewer_username") or context["username"]),
                        include_all=include_all,
                    ),
                    "scope": "all" if include_all else "owned",
                },
            )
            return

        def _do_WS_upgrade(self) -> None:
            from runtime.ws_bridge import compute_accept_key
            from runtime.ws_peer_handler import handle_peer_connection

            upgrade = str(self.headers.get("Upgrade", "")).strip().lower()
            if upgrade != "websocket":
                self._json(400, {"error": "websocket_upgrade_required"})
                return
            ws_key = str(self.headers.get("Sec-WebSocket-Key", "")).strip()
            if not ws_key:
                self._json(400, {"error": "sec_websocket_key_required"})
                return
            accept_key = compute_accept_key(ws_key)
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept_key)
            self.end_headers()
            # Block here until the WebSocket session ends
            self.close_connection = True
            handle_peer_connection(
                rfile=self.rfile,
                wfile=self.wfile,
                runtime_root=runtime_root,
                manifest=manifest,
                self_service=self_service,
                process_id=process_id,
                log_path=log_path,
                append_history=append_history,
                verify_user_password=verify_user_password,
                list_peer_joinable_sessions=list_peer_joinable_sessions,
                register_history_subscriber=register_history_subscriber,
                unregister_history_subscriber=unregister_history_subscriber,
                record_session_agent_contact=record_session_agent_contact,
                write_jsonl=write_jsonl,
            )

        def do_POST(self) -> None:
            path, _query = request_parts(self)
            if path != "/" and path.endswith("/"):
                path = path.rstrip("/")
            content_type = self.headers.get("Content-Type", "")
            length = int(self.headers.get("Content-Length", "0"))
            # Multipart file uploads are handled separately (binary safe)
            if "multipart/form-data" in content_type and path == "/session/goal/attach":
                raw_bytes = self.rfile.read(length) if length else b""
                return self._do_POST_goal_attach_multipart(raw_bytes, content_type)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            payload: dict[str, Any]
            if "application/json" in content_type:
                try:
                    payload = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    self._json(400, {"error": "invalid_json"})
                    return
            else:
                from urllib.parse import parse_qs

                form = parse_qs(raw, keep_blank_values=True)
                payload = {key: values[0] for key, values in form.items()}
            if path == "/bootstrap":
                return self._do_POST_bootstrap(payload, content_type)
            if path == "/peer/ping":
                return self._do_POST_peer_ping(payload)
            if path == "/federation/connect":
                return self._do_POST_federation_connect(payload)
            if path == "/federation/message":
                return self._do_POST_federation_message(payload)
            if path == "/register":
                return self._do_POST_register(payload, content_type)
            if path == "/login":
                return self._do_POST_login(payload, content_type)
            if path == "/logout":
                return self._do_POST_logout(content_type)
            if path == "/sessions":
                return self._do_POST_sessions(payload, content_type)
            if path == "/session/select":
                return self._do_POST_session_select(payload, content_type)
            if path == "/session/rename":
                return self._do_POST_session_rename(payload)
            if path == "/compact":
                return self._do_POST_compact(payload, content_type)
            if path == "/usage":
                return self._do_POST_usage(payload)
            if path == "/session/auto-compact-threshold":
                return self._do_POST_session_auto_compact_threshold(payload)
            if path == "/session/goal":
                return self._do_POST_session_goal(payload)
            if path == "/session/goal/state":
                return self._do_POST_session_goal_state(payload)
            if path == "/session/goal/attach":
                # Reached here only if not multipart (fallback error)
                self._json(400, {"error": "multipart_form_data_required"})
                return
            if path == "/service/control":
                return self._do_POST_service_control(payload)
            if path == "/session/agent/welcome":
                return self._do_POST_session_agent_welcome(payload)
            if path == "/session/peer-joinable":
                return self._do_POST_session_peer_joinable(payload)
            if path == "/session/selected-agents":
                return self._do_POST_session_selected_agents(payload)
            if path == "/session/agent-file/write":
                return self._do_POST_agent_file_write(payload)
            if path == "/session/agent-file/delete":
                return self._do_POST_agent_file_delete(payload)
            if path == "/session/agent-file/acl":
                return self._do_POST_agent_file_acl(payload)
            if path != "/message":
                self._json(404, {"error": "not_found"})
                return
            return self._do_POST_message(payload, content_type)

        def _do_POST_bootstrap(self, payload: dict, content_type: str) -> None:
            password = str(payload.get("password", ""))
            ok, result = bootstrap_root_user(runtime_root, password=password)
            if not ok:
                self._json(400, {"error": result})
                return
            token = create_session(runtime_root, username=result)
            if "application/json" in content_type:
                self._json_with_cookie(201, {"ok": True, "username": result, "role": "superuser"}, token)
                return
            self._redirect("/", token=token)
            return

        def _do_POST_peer_ping(self, payload: dict) -> None:
            peer_url = str(payload.get("peer_url", "")).strip().rstrip("/")
            if not peer_url:
                self._json(400, {"error": "peer_url_required"})
                return
            try:
                with urllib.request.urlopen(f"{peer_url}/peer/ping", timeout=3) as response:
                    upstream = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                self._json(502, {"error": "peer_unreachable", "detail": str(exc), "peer_url": peer_url})
                return
            self._json(
                200,
                {
                    "ok": True,
                    "from": peer_descriptor(),
                    "to": upstream,
                },
            )
            return

        def _do_POST_federation_connect(self, payload: dict) -> None:
            node_id = str(payload.get("node_id", "")).strip()
            peer_id = str(payload.get("peer_id", "")).strip() or None
            base_url = str(payload.get("base_url", "")).strip().rstrip("/")
            started_at = str(payload.get("started_at", "")).strip() or None
            reciprocal = str(payload.get("reciprocal", "true")).lower() != "false"
            if not node_id or not base_url:
                self._json(400, {"error": "node_id_and_base_url_required"})
                return
            peer = register_peer(
                runtime_root,
                node_id=node_id,
                peer_id=peer_id,
                base_url=base_url,
                started_at=started_at,
            )
            if reciprocal:
                local_peer = peer_descriptor()
                request = urllib.request.Request(
                    url=f"{base_url}/federation/connect",
                    data=json.dumps(
                        {
                            "node_id": local_peer["node_id"],
                            "peer_id": local_peer["peer_id"],
                            "started_at": local_peer["started_at"],
                            "base_url": local_peer["base_url"],
                            "reciprocal": False,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        reciprocal_result = json.loads(response.read().decode("utf-8"))
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                    self._json(502, {"error": "reciprocal_connect_failed", "detail": str(exc), "peer": peer})
                    return
                self._json(200, {"ok": True, "peer": peer, "reciprocal": reciprocal_result})
                return
            self._json(200, {"ok": True, "peer": peer})
            return

        def _do_POST_federation_message(self, payload: dict) -> None:
            message = payload.get("message")
            if not isinstance(message, dict):
                self._json(400, {"error": "message_required"})
                return
            if send_router_control(message):
                self._json(
                    202,
                    {
                        "accepted": True,
                        "to": message.get("to"),
                        "to_node": message_meta_get(message, "to_node"),
                    },
                )
            else:
                self._json(
                    503,
                    {
                        "accepted": False,
                        "error": "router_control_injection_failed",
                        "to": message.get("to"),
                        "to_node": message_meta_get(message, "to_node"),
                    },
                )
            return

        def _do_POST_register(self, payload: dict, content_type: str) -> None:
            if not has_users(runtime_root):
                self._json(400, {"error": "bootstrap_required"})
                return
            context = current_context(self, payload=payload)
            if not context or not bool(context.get("is_superuser")):
                self._json(403, {"error": "superuser_required"})
                return
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
            ok, result = create_user(runtime_root, username=username, password=password)
            if not ok:
                self._json(400, {"error": result})
                return
            if "application/json" in content_type:
                self._json(201, {"ok": True, "username": result, "role": "user"})
                return
            self._redirect("/")
            return

        def _do_POST_login(self, payload: dict, content_type: str) -> None:
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
            requested_login_session_id = requested_session_id(self, payload=payload)
            self._trace_auth_request(phase="login_attempt", path="/login", username=username)
            if not verify_user_password(runtime_root, username=username, password=password):
                self._trace_auth_request(phase="login_rejected", path="/login", username=username)
                self._json(401, {"error": "invalid_credentials"})
                return
            token = create_session(runtime_root, username=username)
            self._trace_auth_request(
                phase="login_accepted",
                path="/login",
                username=username,
                context=resolve_session_context(runtime_root, token),
            )
            if "application/json" in content_type:
                response_payload = {"ok": True, "username": username.strip().lower()}
                if requested_login_session_id:
                    response_payload["session_id"] = requested_login_session_id
                self._json_with_cookie(200, response_payload, token)
                return
            location = "/"
            if requested_login_session_id:
                location = f"/?{urlencode({'session_id': requested_login_session_id})}"
            self._redirect(location, token=token)
            return

        def _do_POST_logout(self, content_type: str) -> None:
            delete_session(runtime_root, cookie_value("bridge_session", self.headers.get("Cookie")))
            if "application/json" in content_type:
                self._json_with_cookie(200, {"ok": True}, None)
                return
            self._redirect("/", token=None)
            return

        def _do_POST_sessions(self, payload: dict, content_type: str) -> None:
            context = self._require_user(payload=payload)
            if not context:
                return
            label = str(payload.get("label", "")).strip() or None
            parent_session_id = str(payload.get("parent_session_id") or context["session_id"] or "").strip()
            parent_talk = get_session_settings(
                runtime_root,
                username=context["username"],
                session_id=parent_session_id,
            ) or {}
            talk = create_child_conversation_session(
                runtime_root,
                username=context["username"],
                parent_session_id=parent_session_id,
                label=label,
                created_by_username=context["username"],
                created_by_type="user",
                origin_session_id=parent_session_id,
                origin_goal_id=str(parent_talk.get("active_goal_id") or parent_talk.get("goal_id") or "").strip(),
                origin_goal_text=str(parent_talk.get("goal_text") or ""),
            )
            if not talk:
                self._json(404, {"error": "parent_session_not_found"})
                return
            with _ov_cache_lock:
                _ov_cache_state[0] = None  # invalidate so next /overview reflects the new session
            if "application/json" in content_type:
                self._json(
                    201,
                    {
                        "ok": True,
                        "session": talk,
                        "active_session_id": talk["session_id"],
                    },
                )
                return
            self._redirect(f"/?{urlencode({'session_id': str(talk['session_id'])})}")
            return

        def _do_POST_session_select(self, payload: dict, content_type: str) -> None:
            context = self._require_user(payload=payload)
            if not context:
                return
            session_id = str(payload.get("session_id", "")).strip() or str(payload.get("session_id", "")).strip()
            if not session_id:
                self._json(400, {"error": "session_id_required"})
                return
            sessions = list_sessions(runtime_root, username=context["username"])
            if not any(str(talk.get("session_id") or talk.get("session_id")) == session_id for talk in sessions):
                self._json(404, {"error": "session_not_found"})
                return
            select_session(
                runtime_root,
                token=cookie_value("bridge_session", self.headers.get("Cookie")) or "",
                session_id=session_id,
            )
            if "application/json" in content_type:
                self._json(200, {"ok": True, "username": context["username"], "session_id": session_id})
                return
            self._redirect(f"/?{urlencode({'session_id': session_id})}")
            return

        def _do_POST_session_rename(self, payload: dict) -> None:
            context = self._require_user(payload=payload)
            if not context:
                return
            rename_session_id = (
                str(payload.get("session_id", "")).strip()
                or str(payload.get("session_id", "")).strip()
                or context["session_id"]
            )
            rename_label = str(payload.get("label", "")).strip()
            if not rename_session_id:
                self._json(400, {"error": "session_id_required"})
                return
            if not rename_label:
                self._json(400, {"error": "label_required"})
                return
            renamed = rename_session(runtime_root, username=context["username"], session_id=rename_session_id, label=rename_label)
            if not renamed:
                self._json(404, {"error": "session_not_found"})
                return
            self._json(200, {"ok": True, "session": renamed})
            return

        def _do_POST_compact(self, payload: dict, content_type: str) -> None:
            context = self._require_user(payload=payload)
            if not context:
                return
            status, response = manual_compact_current_session(
                username=context["username"],
                session_id=context["session_id"],
            )
            if "application/json" in content_type:
                self._json(status, response)
                return
            self._redirect(f"/?{urlencode({'session_id': context['session_id']})}")
            return

        def _do_POST_usage(self, payload: dict) -> None:
            context = self._require_user(payload=payload)
            if not context:
                return
            talk = get_session_settings(runtime_root, username=context["username"], session_id=context["session_id"])
            if not talk:
                self._json(404, {"error": "session_not_found"})
                return
            ctx_status = talk.get("last_context_status") if isinstance(talk, dict) else None
            ctx_updated_at = talk.get("last_context_status_updated_at") if isinstance(talk, dict) else None
            auto_compact_threshold = int(talk.get("auto_compact_threshold_left_percent", 30)) if isinstance(talk, dict) else 30
            target_service_id = get_session_service(runtime_root, username=context["username"], session_id=context["session_id"])
            provider = llm_service_kinds.get(target_service_id) if target_service_id else None
            self._json(
                200,
                {
                    "ok": True,
                    "session_id": context["session_id"],
                    "session_id": context["session_id"],
                    "provider": provider or "unknown",
                    "context_status": ctx_status,
                    "context_status_updated_at": ctx_updated_at,
                    "auto_compact_threshold_left_percent": auto_compact_threshold,
                    "left_percent": ctx_status.get("left_percent") if isinstance(ctx_status, dict) else None,
                    "used_percent": ctx_status.get("used_percent") if isinstance(ctx_status, dict) else None,
                    "label": ctx_status.get("label") if isinstance(ctx_status, dict) else "No context data yet",
                },
            )
            return

        def _do_POST_session_auto_compact_threshold(self, payload: dict) -> None:
            context = self._require_user(payload=payload)
            if not context:
                return
            threshold_left_percent = normalize_auto_compact_threshold_left_percent(
                payload.get("threshold_left_percent")
            )
            talk = update_session_auto_compact_threshold(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                threshold_left_percent=threshold_left_percent,
            )
            if not talk:
                self._json(404, {"error": "session_not_found"})
                return
            self._json(
                200,
                {
                    "ok": True,
                    "session_id": context["session_id"],
                    "session_id": context["session_id"],
                    "threshold_left_percent": int(
                        talk.get("auto_compact_threshold_left_percent", threshold_left_percent)
                    ),
                },
            )
            return

        def _do_POST_session_goal(self, payload: dict) -> None:
            context = self._require_user(payload=payload)
            if not context:
                return
            talk = get_session_settings(runtime_root, username=context["username"], session_id=context["session_id"])
            if not session_operation_allowed(talk, "update_goal"):
                self._json(403, {"error": "goal_update_disabled"})
                return
            write_jsonl(
                log_path,
                {
                    "type": "http.goal_update_received",
                    "ts": utc_ts(),
                    "service_id": self_service["service_id"],
                    "process_id": process_id,
                    "username": context["username"],
                    "session_id": context["session_id"],
                },
            )
            old_talk = talk or {}
            previous_goal = str(old_talk.get("goal_text", "")).strip()
            previous_goal_id = str(old_talk.get("active_goal_id") or old_talk.get("goal_id") or "").strip() or None
            talk = update_session_goal(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                goal_text=payload.get("goal_text"),
                updated_by_username=context["username"],
                updated_by_type="user",
                origin_session_id=context["session_id"],
                origin_goal_id=previous_goal_id or "",
                origin_goal_text=previous_goal,
            )
            if not talk:
                self._json(404, {"error": "session_not_found"})
                return
            # New goal clears any stuck agent audit_state so all agents can be re-dispatched
            reset_agent_audit_states_for_session(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
            )
            dispatched_to, dispatch_error = enqueue_goal_dispatch(
                username=context["username"],
                session_id=context["session_id"],
                auth_context=issue_auth_context(runtime_root, username=context["username"]),
                reason="goal_saved",
                previous_goal_text=previous_goal,
                previous_goal_id=previous_goal_id,
            )
            self._json(
                200,
                {
                    **goal_state_response_payload(
                        talk,
                        session_id=context["session_id"],
                        default_provider=default_provider,
                        **_goal_manager_runtime_payload(
                            username=context["username"],
                            session_id=context["session_id"],
                            bound_service_id=get_session_service(
                                runtime_root,
                                username=context["username"],
                                session_id=context["session_id"],
                            ),
                        ),
                    ),
                    "dispatched_to": dispatched_to,
                    "dispatch_error": dispatch_error,
                },
            )
            return

        def _do_POST_session_goal_state(self, payload: dict) -> None:
            context = self._require_user(payload=payload)
            if not context:
                return
            current_talk = get_session_settings(runtime_root, username=context["username"], session_id=context["session_id"])
            if not current_talk:
                self._json(404, {"error": "session_not_found"})
                return
            if (
                any(key in payload for key in ("goal_active", "goal_completed"))
                and not session_operation_allowed(current_talk, "update_goal")
            ):
                self._json(403, {"error": "goal_update_disabled"})
                return
            write_jsonl(
                log_path,
                {
                    "type": "http.goal_state_update_received",
                    "ts": utc_ts(),
                    "service_id": self_service["service_id"],
                    "process_id": process_id,
                    "username": context["username"],
                    "session_id": context["session_id"],
                },
            )
            previous_bound_service_id = get_session_service(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
            )
            talk = update_session_goal_flags(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                goal_active=payload.get("goal_active") if "goal_active" in payload else None,
                goal_completed=payload.get("goal_completed") if "goal_completed" in payload else None,
                goal_reset_completed_on_prompt=(
                    payload.get("goal_reset_completed_on_prompt")
                    if "goal_reset_completed_on_prompt" in payload
                    else None
                ),
                goal_auto_compact_enabled=(
                    payload.get("goal_auto_compact_enabled")
                    if "goal_auto_compact_enabled" in payload
                    else None
                ),
                agent_welcome_enabled=(
                    payload.get("agent_welcome_enabled")
                    if "agent_welcome_enabled" in payload
                    else None
                ),
                preferred_provider=payload.get("preferred_provider") if "preferred_provider" in payload else None,
                auto_resume_enabled=payload.get("auto_resume_enabled") if "auto_resume_enabled" in payload else None,
                auto_resume_interval_seconds=(
                    payload.get("auto_resume_interval_seconds")
                    if "auto_resume_interval_seconds" in payload
                    else None
                ),
                agent_priority=payload.get("agent_priority") if "agent_priority" in payload else None,
                session_priority=payload.get("session_priority") if "session_priority" in payload else None,
            )
            if not talk:
                self._json(404, {"error": "session_not_found"})
                return
            requested_provider = (
                str(payload.get("preferred_provider")).strip().lower()
                if "preferred_provider" in payload
                else ""
            )
            released_for_provider_switch: str | None = None
            if requested_provider in {"codex", "claude"} and previous_bound_service_id:
                previous_kind = str(llm_service_kinds.get(previous_bound_service_id) or "").strip().lower()
                if previous_kind and previous_kind != requested_provider:
                    released_for_provider_switch = release_session_service(
                        runtime_root,
                        username=context["username"],
                        session_id=context["session_id"],
                    )
                    clear_session_service_runtime(
                        runtime_root,
                        username=context["username"],
                        session_id=context["session_id"],
                        service_id=previous_bound_service_id,
                    )
            released_service_id = maybe_release_session_provider(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                talk=talk,
            )
            runnable_goal = bool(talk.get("goal_active", False)) and not bool(talk.get("goal_completed", False)) and (
                str(talk.get("goal_progress_state", "in_progress")).strip().lower() == "in_progress"
            )
            if requested_provider in {"codex", "claude"} and runnable_goal:
                provider_pool = codex_service_pool if requested_provider == "codex" else claude_service_pool
                leased_service_id = lease_session_service(
                    runtime_root,
                    username=context["username"],
                    session_id=context["session_id"],
                    pool_service_ids=provider_pool,
                )
                if leased_service_id:
                    record_session_agent_contact(
                        runtime_root,
                        username=context["username"],
                        session_id=context["session_id"],
                        service_id=leased_service_id,
                        provider=requested_provider,
                    )
            dispatched_to, dispatch_error = enqueue_goal_dispatch(
                username=context["username"],
                session_id=context["session_id"],
                auth_context=issue_auth_context(runtime_root, username=context["username"]),
                reason="goal_state_changed",
            )
            self._json(
                200,
                {
                    **goal_state_response_payload(
                        talk,
                        session_id=context["session_id"],
                        default_provider=default_provider,
                        **_goal_manager_runtime_payload(
                            username=context["username"],
                            session_id=context["session_id"],
                            bound_service_id=get_session_service(
                                runtime_root,
                                username=context["username"],
                                session_id=context["session_id"],
                            ),
                        ),
                    ),
                    "dispatched_to": dispatched_to,
                    "dispatch_error": dispatch_error,
                    "released_service_id": released_service_id or released_for_provider_switch,
                },
            )
            return

        def _do_POST_goal_attach_multipart(self, raw_bytes: bytes, content_type: str) -> None:
            context = self._require_user()
            if not context:
                return
            # Parse boundary from Content-Type header
            boundary = ""
            for part in content_type.split(";"):
                part = part.strip()
                if part.startswith("boundary="):
                    boundary = part[len("boundary="):].strip().strip('"')
                    break
            if not boundary:
                self._json(400, {"error": "missing_multipart_boundary"})
                return
            # Parse multipart parts
            parts = _parse_multipart_bytes(raw_bytes, boundary)
            talk = get_session_settings(
                runtime_root, username=context["username"], session_id=context["session_id"]
            )
            if not talk:
                self._json(404, {"error": "session_not_found"})
                return
            goal_id = str(talk.get("active_goal_id") or talk.get("goal_id") or "").strip()
            if not goal_id:
                self._json(400, {"error": "no_active_goal"})
                return
            saved: list[dict[str, Any]] = []
            for part in parts:
                name = part.get("name", "")
                if name != "file":
                    continue
                filename = part.get("filename") or "attachment"
                data = part.get("data", b"")
                if not data:
                    continue
                stored_name = save_goal_attachment(
                    runtime_root,
                    username=context["username"],
                    session_id=context["session_id"],
                    goal_id=goal_id,
                    filename=filename,
                    data=data,
                )
                saved.append({"filename": stored_name, "size": len(data)})
            attachments = list_goal_attachments(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                goal_id=goal_id,
            )
            self._json(200, {"ok": True, "saved": saved, "attachments": attachments, "goal_id": goal_id})

        def _do_GET_goal_attachments(self, _path: str, query: dict) -> None:
            context = self._require_user(query=query)
            if not context:
                return
            talk = get_session_settings(
                runtime_root, username=context["username"], session_id=context["session_id"]
            )
            if not talk:
                self._json(404, {"error": "session_not_found"})
                return
            goal_id = str(query.get("goal_id") or talk.get("active_goal_id") or talk.get("goal_id") or "").strip()
            if not goal_id:
                self._json(200, {"ok": True, "attachments": [], "goal_id": ""})
                return
            attachments = list_goal_attachments(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                goal_id=goal_id,
            )
            self._json(200, {"ok": True, "attachments": attachments, "goal_id": goal_id})

        def _do_GET_goal_attachment(self, _path: str, query: dict) -> None:
            from runtime.persistent_state_pkg import session_goal_attachments_dir, normalize_username
            context = self._require_user(query=query)
            if not context:
                return
            goal_id = str(query.get("goal_id") or "").strip()
            filename = str(query.get("filename") or "").strip()
            if not goal_id or not filename or "/" in filename or "\\" in filename or filename.startswith("."):
                self._json(400, {"error": "invalid_params"})
                return
            attachments_dir = session_goal_attachments_dir(
                runtime_root,
                username=normalize_username(context["username"]),
                session_id=context["session_id"],
                goal_id=goal_id,
            )
            file_path = attachments_dir / filename
            if not file_path.exists() or not file_path.is_file():
                self._json(404, {"error": "not_found"})
                return
            data = file_path.read_bytes()
            from runtime.persistent_state_pkg._core import _guess_attachment_content_type
            ct = _guess_attachment_content_type(filename)
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(data)

        def _do_POST_service_control(self, payload: dict) -> None:
            context = self._require_user()
            if not context:
                return
            auth_context = issue_auth_context(runtime_root, username=context["username"])
            if not auth_context or not auth_context_allows(auth_context, "control_service"):
                self._json(403, {"error": "control_service_required"})
                return
            action = str(payload.get("action", "")).strip().lower()
            service_id = str(payload.get("service_id", "")).strip()
            if action not in {"start", "stop", "restart", "reload", "status"}:
                self._json(400, {"error": "unsupported_action"})
                return
            if not service_id:
                self._json(400, {"error": "service_id_required"})
                return
            if action == "status":
                service = get_service_record(runtime_root, service_id)
                lifecycle = load_lifecycle_state(runtime_root).get("processes", {})
                process = lifecycle.get(str(service.get("current_process_id"))) if service.get("current_process_id") else None
                self._json(200, {"ok": True, "service": service, "process": process})
                return
            enqueue_service_control(action=action, service_id=service_id, auth_context=auth_context)
            self._json(202, {"accepted": True, "action": action, "service_id": service_id})
            return

        def _do_POST_session_agent_welcome(self, payload: dict) -> None:
            context = self._require_user(payload=payload)
            if not context:
                return
            service_id = str(payload.get("service_id", "")).strip()
            if not service_id:
                self._json(400, {"error": "service_id_required"})
                return
            service_record = get_service_record(runtime_root, service_id) or {}
            provider = str(service_record.get("kind", "")).strip()
            result = record_session_agent_contact(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                service_id=service_id,
                provider=provider,
            )
            if not result:
                self._json(404, {"error": "session_not_found"})
                return
            write_jsonl(
                log_path,
                {
                    "type": "http.agent_welcomed",
                    "ts": utc_ts(),
                    "service_id": self_service["service_id"],
                    "process_id": process_id,
                    "username": context["username"],
                    "session_id": context["session_id"],
                    "welcomed_service_id": service_id,
                    "provider": provider,
                },
            )
            welcomed_agents = result.get("welcomed_agents", [])
            self._json(200, {"ok": True, "service_id": service_id, "provider": provider, "welcomed_agents": welcomed_agents})
            return

        def _do_POST_session_peer_joinable(self, payload: dict) -> None:
            context = self._require_user(payload=payload)
            if not context:
                return
            username = context["username"]
            session_id = context["session_id"]
            raw_flag = payload.get("peer_joinable")
            if raw_flag is None:
                self._json(400, {"error": "peer_joinable_required"})
                return
            flag = bool(raw_flag) if isinstance(raw_flag, bool) else str(raw_flag).lower() in {"true", "1", "yes"}
            result = update_session_peer_joinable(
                runtime_root,
                username=username,
                session_id=session_id,
                peer_joinable=flag,
            )
            if not result:
                self._json(404, {"error": "session_not_found"})
                return
            self._json(200, {"ok": True, "session_id": session_id, "peer_joinable": flag})

        def _do_POST_session_selected_agents(self, payload: dict) -> None:
            context = self._require_user(payload=payload)
            if not context:
                return
            username = context["username"]
            session_id = context["session_id"]
            selected_agents = payload.get("selected_agents")
            if not isinstance(selected_agents, list):
                self._json(400, {"error": "selected_agents_list_required"})
                return
            result = update_session_selected_agents(
                runtime_root,
                username=username,
                session_id=session_id,
                selected_agents=selected_agents,
            )
            if not result:
                self._json(404, {"error": "session_not_found"})
                return
            self._json(200, {"ok": True, "session_id": session_id, "selected_agents": result.get("selected_agents", [])})

        # ------------------------------------------------------------------ agent file endpoints

        def _agent_file_acl_check(
            self,
            context: dict,
            agent_id: str,
            permission: str,
        ) -> bool:
            """Return True if the HTTP caller may perform ``permission`` on ``agent_id``'s directory.

            HTTP callers that do NOT supply a ``caller_agent_id`` are treated as
            session admin (the session owner) and always allowed.  If the session
            has the superuser role the check is also bypassed.
            The ``caller_agent_id`` key in the context dict is populated by the
            handlers that extract it from the request payload / query.
            """
            is_superuser = bool(context.get("is_superuser"))
            caller_agent_id = str(context.get("caller_agent_id") or "").strip()
            if is_superuser or not caller_agent_id:
                return True
            return check_agent_file_acl(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                dir_agent_id=agent_id,
                caller_agent_id=caller_agent_id,
                permission=permission,
            )

        def _do_POST_agent_file_write(self, payload: dict) -> None:
            """Write a file to an agent's inbox or outbox.

            Body (JSON):
              agent_id         – target directory's agent_id
              filename         – target filename (basename only; path traversal is rejected)
              box              – "inbox" or "outbox"
              content_b64      – base64-encoded file content
              caller_agent_id  – (optional) identity of the writing agent; enforces ACL when set
            """
            import base64
            context = self._require_user(payload=payload)
            if not context:
                return
            agent_id = str(payload.get("agent_id", "")).strip()
            filename = str(payload.get("filename", "")).strip()
            box = str(payload.get("box", "")).strip().lower()
            content_b64 = str(payload.get("content_b64", "")).strip()
            context["caller_agent_id"] = str(payload.get("caller_agent_id", "")).strip()
            if not agent_id:
                self._json(400, {"error": "agent_id_required"})
                return
            if not filename:
                self._json(400, {"error": "filename_required"})
                return
            if box not in {"inbox", "outbox"}:
                self._json(400, {"error": "box_must_be_inbox_or_outbox"})
                return
            if not content_b64:
                self._json(400, {"error": "content_b64_required"})
                return
            if not self._agent_file_acl_check(context, agent_id, "write"):
                self._json(403, {"error": "access_denied"})
                return
            try:
                content = base64.b64decode(content_b64)
            except Exception:
                self._json(400, {"error": "invalid_base64"})
                return
            ok = write_agent_file(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                agent_id=agent_id,
                box=box,
                filename=filename,
                content=content,
            )
            if not ok:
                self._json(400, {"error": "write_failed"})
                return
            self._json(200, {"ok": True, "agent_id": agent_id, "box": box, "filename": filename, "size": len(content)})

        def _do_POST_agent_file_delete(self, payload: dict) -> None:
            """Delete a file from an agent's inbox or outbox.

            Body (JSON): agent_id, filename, box, caller_agent_id (optional)
            """
            context = self._require_user(payload=payload)
            if not context:
                return
            agent_id = str(payload.get("agent_id", "")).strip()
            filename = str(payload.get("filename", "")).strip()
            box = str(payload.get("box", "")).strip().lower()
            context["caller_agent_id"] = str(payload.get("caller_agent_id", "")).strip()
            if not agent_id or not filename or box not in {"inbox", "outbox"}:
                self._json(400, {"error": "agent_id_filename_and_box_required"})
                return
            if not self._agent_file_acl_check(context, agent_id, "write"):
                self._json(403, {"error": "access_denied"})
                return
            deleted = delete_agent_file(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                agent_id=agent_id,
                box=box,
                filename=filename,
            )
            self._json(200, {"ok": True, "deleted": deleted})

        def _do_GET_agent_file_list(self, _path: str, query: dict) -> None:
            """List files in an agent's inbox or outbox.

            Query params: agent_id, box (inbox|outbox), caller_agent_id (optional)
            """
            context = self._require_user(query=query)
            if not context:
                return
            agent_id = (query.get("agent_id") or [""])[0].strip()
            box = (query.get("box") or [""])[0].strip().lower()
            context["caller_agent_id"] = (query.get("caller_agent_id") or [""])[0].strip()
            if not agent_id or box not in {"inbox", "outbox"}:
                self._json(400, {"error": "agent_id_and_box_required"})
                return
            if not self._agent_file_acl_check(context, agent_id, "read"):
                self._json(403, {"error": "access_denied"})
                return
            files = list_agent_files(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                agent_id=agent_id,
                box=box,
            )
            self._json(200, {"ok": True, "agent_id": agent_id, "box": box, "files": files})

        def _do_GET_agent_file_read(self, _path: str, query: dict) -> None:
            """Read (download) a file from an agent's inbox or outbox.

            Query params: agent_id, box (inbox|outbox), filename, caller_agent_id (optional)
            Returns JSON: {ok, agent_id, box, filename, size, content_b64}
            """
            import base64
            context = self._require_user(query=query)
            if not context:
                return
            agent_id = (query.get("agent_id") or [""])[0].strip()
            box = (query.get("box") or [""])[0].strip().lower()
            filename = (query.get("filename") or [""])[0].strip()
            context["caller_agent_id"] = (query.get("caller_agent_id") or [""])[0].strip()
            if not agent_id or box not in {"inbox", "outbox"} or not filename:
                self._json(400, {"error": "agent_id_box_and_filename_required"})
                return
            if not self._agent_file_acl_check(context, agent_id, "read"):
                self._json(403, {"error": "access_denied"})
                return
            content = read_agent_file(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                agent_id=agent_id,
                box=box,
                filename=filename,
            )
            if content is None:
                self._json(404, {"error": "file_not_found"})
                return
            self._json(200, {
                "ok": True,
                "agent_id": agent_id,
                "box": box,
                "filename": filename,
                "size": len(content),
                "content_b64": base64.b64encode(content).decode("ascii"),
            })

        def _do_GET_agent_file_acl(self, _path: str, query: dict) -> None:
            """Get the ACL for an agent file directory.

            Query params: agent_id
            """
            context = self._require_user(query=query)
            if not context:
                return
            agent_id = (query.get("agent_id") or [""])[0].strip()
            if not agent_id:
                self._json(400, {"error": "agent_id_required"})
                return
            acl = get_agent_file_dir_acl(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                agent_id=agent_id,
            )
            self._json(200, {"ok": True, "agent_id": agent_id, "acl": acl})

        def _do_POST_agent_file_acl(self, payload: dict) -> None:
            """Set (update) the ACL for an agent file directory.

            Body (JSON):
              agent_id – the target directory's agent_id (required)
              owner    – new owner agent_id (optional)
              grants   – list of {agent_id, permissions:[\"read\",\"write\"]} (optional)

            Only the session owner (HTTP user) or a superuser may modify the ACL.
            """
            context = self._require_user(payload=payload)
            if not context:
                return
            agent_id = str(payload.get("agent_id", "")).strip()
            if not agent_id:
                self._json(400, {"error": "agent_id_required"})
                return
            owner = payload.get("owner")
            grants = payload.get("grants")
            if owner is not None:
                owner = str(owner).strip()
            if grants is not None and not isinstance(grants, list):
                self._json(400, {"error": "grants_must_be_list"})
                return
            acl = set_agent_file_dir_acl(
                runtime_root,
                username=context["username"],
                session_id=context["session_id"],
                agent_id=agent_id,
                owner=owner,
                grants=grants,
            )
            self._json(200, {"ok": True, "agent_id": agent_id, "acl": acl})

        def _do_POST_message(self, payload: dict, content_type: str) -> None:
            context = self._require_user(payload=payload)
            if not context:
                return
            username = context["username"]
            session_id = context["session_id"]
            talk = get_session_settings(runtime_root, username=username, session_id=session_id)
            if not talk:
                self._json(404, {"error": "session_not_found"})
                return
            mode = str(payload.get("mode", "prompt")).strip().lower() or "prompt"
            auth_context = issue_auth_context(runtime_root, username=username)
            provider_override = str(payload.get("provider", "")).strip().lower()
            if provider_override in {"codex", "claude"}:
                update_session_goal_flags(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    preferred_provider=provider_override,
                )
            payload.setdefault("to", default_target)
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                self._json(400, {"error": "text_required"})
                return
            if mode == "goal":
                if not session_operation_allowed(talk, "update_goal"):
                    self._json(403, {"error": "goal_update_disabled"})
                    return
                def process_goal_dispatch() -> None:
                    try:
                        write_jsonl(
                            log_path,
                            {
                                "type": "http.goal_update_received",
                                "ts": utc_ts(),
                                "service_id": self_service["service_id"],
                                "process_id": process_id,
                                "username": username,
                                "session_id": session_id,
                            },
                        )
                        old_talk = get_session_settings(
                            runtime_root,
                            username=username,
                            session_id=session_id,
                        ) or {}
                        previous_goal = str(old_talk.get("goal_text", "")).strip()
                        previous_goal_id = (
                            str(old_talk.get("active_goal_id") or old_talk.get("goal_id") or "").strip() or None
                        )
                        talk = update_session_goal(
                            runtime_root,
                            username=username,
                            session_id=session_id,
                            goal_text=text,
                            updated_by_username=username,
                            updated_by_type="user",
                            origin_session_id=session_id,
                            origin_goal_id=previous_goal_id or "",
                            origin_goal_text=previous_goal,
                        )
                        if not talk:
                            write_jsonl(
                                log_path,
                                {
                                    "type": "http.goal_update_missing_session",
                                    "ts": utc_ts(),
                                    "service_id": self_service["service_id"],
                                    "process_id": process_id,
                                    "username": username,
                                    "session_id": session_id,
                                },
                            )
                            return
                        reset_agent_audit_states_for_session(
                            runtime_root,
                            username=username,
                            session_id=session_id,
                        )
                        dispatched_to, dispatch_error = enqueue_goal_dispatch(
                            username=username,
                            session_id=session_id,
                            auth_context=auth_context,
                            reason="goal_saved",
                            previous_goal_text=previous_goal,
                            previous_goal_id=previous_goal_id,
                        )
                        write_jsonl(
                            log_path,
                            {
                                "type": "http.goal_dispatch_enqueued",
                                "ts": utc_ts(),
                                "service_id": self_service["service_id"],
                                "process_id": process_id,
                                "username": username,
                                "session_id": session_id,
                                "to": dispatched_to,
                                "dispatch_error": dispatch_error,
                            },
                        )
                    except Exception as exc:
                        write_jsonl(
                            log_path,
                            {
                                "type": "http.goal_dispatch_failed",
                                "ts": utc_ts(),
                                "service_id": self_service["service_id"],
                                "process_id": process_id,
                                "username": username,
                                "session_id": session_id,
                                "error": repr(exc),
                            },
                        )

                threading.Thread(target=process_goal_dispatch, daemon=True).start()
                self._json(
                    202,
                    {
                        "ok": True,
                        "mode": "goal",
                        "username": username,
                        "session_id": session_id,
                        "session_id": session_id,
                        "goal_text": text.strip(),
                        "goal_active": bool(text.strip()),
                        "goal_completed": False,
                        "goal_reset_completed_on_prompt": True,
                        "agent_welcome_enabled": False,
                        "preferred_provider": provider_override or "auto",
                        "dispatched_to": None,
                        "dispatch_error": None,
                        "queued": True,
                    },
                )
                return
            if mode not in {"prompt", ""}:
                self._json(400, {"error": "unsupported_mode"})
                return
            session_owner_username = str(talk.get("username") or "").strip()
            if session_owner_username and session_owner_username != username:
                self._json(403, {"error": "session_owner_required"})
                return
            if not session_operation_allowed(talk, "send_prompt"):
                self._json(403, {"error": "prompt_disabled"})
                return
            requested_to_service = payload.get("to", default_target)
            if not isinstance(requested_to_service, str) or not requested_to_service:
                self._json(400, {"error": "to_required"})
                return
            prompt_text = text.strip()

            def process_prompt_submission() -> None:
                dispatch_error: str | None = None
                to_service: str | None = None
                try:
                    previous_session_settings = get_session_settings(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                    ) or {}
                    if bool(previous_session_settings.get("user_response_wait_active", False)):
                        update_session_user_response_wait(
                            runtime_root,
                            username=username,
                            session_id=session_id,
                            active=False,
                            cleared_reason="user_reply",
                        )
                        append_history(
                            username,
                            session_id,
                            {
                                "direction": "event",
                                "ts": utc_ts(),
                                "service_id": self_service["service_id"],
                                "event_type": "service.user_response_wait_cleared",
                                "text": "User replied to the pending question.",
                                "event": {
                                    "type": "service.user_response_wait_cleared",
                                    "reason": "user_reply",
                                },
                            },
                        )
                    if provider_override in {"codex", "claude"}:
                        update_session_goal_flags(
                            runtime_root,
                            username=username,
                            session_id=session_id,
                            preferred_provider=provider_override,
                        )
                    session_settings = get_session_settings(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                    ) or {}
                    preferred_provider = (
                        str(session_settings.get("preferred_provider", default_provider)).strip().lower()
                        or default_provider
                    )
                    current_codex_service_pool, current_claude_service_pool, _current_llm_service_kinds = (
                        current_llm_service_topology()
                    )
                    # selected_agents overrides provider routing when present.
                    # If the list contains only WS-peer service_ids (no pool token and
                    # no individual local service_id) we skip the local LLM entirely.
                    selected_agents_cfg = list(session_settings.get("selected_agents", []))
                    all_local_service_ids = set(current_codex_service_pool) | set(current_claude_service_pool)
                    has_local = any(
                        a in {"codex_pool", "claude_pool"} or a in all_local_service_ids
                        for a in selected_agents_cfg
                    )
                    ws_only_mode = bool(selected_agents_cfg) and not has_local
                    provider_pool = current_codex_service_pool if preferred_provider == "codex" else current_claude_service_pool
                    # When codex_pool or claude_pool is explicitly selected, prefer that pool.
                    if "codex_pool" in selected_agents_cfg:
                        provider_pool = current_codex_service_pool
                    elif "claude_pool" in selected_agents_cfg:
                        provider_pool = current_claude_service_pool
                    else:
                        # When individual local service_ids are selected, restrict pool to those.
                        selected_local = [a for a in selected_agents_cfg if a in all_local_service_ids]
                        if selected_local:
                            provider_pool = selected_local
                    leased_service_id = get_session_service(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                    )
                    if ws_only_mode:
                        # No local LLM worker — WS peer handles this session
                        to_service = None
                    elif requested_to_service == default_target and (current_codex_service_pool or current_claude_service_pool):
                        if leased_service_id and provider_pool and leased_service_id not in provider_pool:
                            leased_service_id = None
                        if not leased_service_id:
                            leased_service_id = lease_session_service(
                                runtime_root,
                                username=username,
                                session_id=session_id,
                                pool_service_ids=provider_pool,
                            )
                        if leased_service_id:
                            to_service = leased_service_id
                        else:
                            dispatch_error = "no_available_provider_worker"
                    else:
                        to_service = requested_to_service
                    target_kind = llm_service_kinds.get(to_service) if isinstance(to_service, str) and to_service else None
                    if str(session_settings.get("goal_text", "")).strip() and bool(
                        session_settings.get("goal_active", False)
                    ):
                        reset_completed = bool(session_settings.get("goal_completed", False)) and bool(
                            session_settings.get("goal_reset_completed_on_prompt", True)
                        )
                        if reset_completed:
                            update_session_goal_flags(
                                runtime_root,
                                username=username,
                                session_id=session_id,
                                goal_completed=False,
                            )
                        reset_agent_audit_states_for_session(
                            runtime_root,
                            username=username,
                            session_id=session_id,
                        )
                    # Determine display target for history entry
                    if ws_only_mode:
                        display_to = "pending:ws_peer"
                    elif to_service:
                        display_to = to_service
                    else:
                        display_to = f"pending:{preferred_provider}"
                    append_history(
                        username,
                        session_id,
                        {
                            "direction": "out",
                            "ts": utc_ts(),
                            "to": display_to,
                            "session_id": session_id,
                            "text": prompt_text,
                            "submitted_by_username": username,
                        },
                    )
                    # When WS-only, write a degraded-state event if no WS peer is
                    # actively subscribed (no live connection joined the session).
                    if ws_only_mode:
                        from runtime.persistent_state_pkg import list_session_agent_contacts
                        _ws_agents = [
                            a for a in list_session_agent_contacts(
                                runtime_root, username=username, session_id=session_id
                            )
                            if str(a.get("provider", "")) == "ws_peer"
                        ]
                        if not _ws_agents:
                            append_history(
                                username,
                                session_id,
                                {
                                    "direction": "event",
                                    "ts": utc_ts(),
                                    "session_id": session_id,
                                    "event_type": "ws_peer.no_agent",
                                    "text": "No WS peer agent is registered for this session. Prompt queued — will be processed on next connection.",
                                    "event": {"type": "ws_peer.no_agent"},
                                },
                            )
                    append_pending_input(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                        entry=make_aize_pending_input(
                            kind="user_message",
                            role="user",
                            text=prompt_text,
                            submitted_by_username=username,
                        ),
                    )
                    maybe_enqueue_mid_turn_progress_inquiry(
                        runtime_root=runtime_root,
                        log_path=log_path,
                        http_service_id=self_service["service_id"],
                        process_id=process_id,
                        username=username,
                        session_id=session_id,
                        source_kind="user_message",
                        source_text=prompt_text,
                        provider=str(target_kind or preferred_provider),
                    )
                    if isinstance(to_service, str) and to_service:
                        if not send_router_control(
                            make_dispatch_pending_message(
                                manifest=manifest,
                                from_service_id=self_service["service_id"],
                                to_service_id=to_service,
                                process_id=process_id,
                                run_id=manifest["run_id"],
                                username=username,
                                session_id=session_id,
                                auth_context=auth_context,
                                reason="http_prompt",
                            )
                        ):
                            dispatch_error = dispatch_error or "router_control_injection_failed"
                    write_jsonl(
                        log_path,
                        {
                            "type": "http.prompt_received",
                            "ts": utc_ts(),
                            "service_id": self_service["service_id"],
                            "process_id": process_id,
                            "username": username,
                            "submitted_by_username": username,
                            "session_id": session_id,
                            "to": to_service,
                            "dispatch_error": dispatch_error,
                        },
                    )
                except Exception as exc:
                    write_jsonl(
                        log_path,
                        {
                            "type": "http.prompt_processing_failed",
                            "ts": utc_ts(),
                            "service_id": self_service["service_id"],
                            "process_id": process_id,
                            "username": username,
                            "submitted_by_username": username,
                            "session_id": session_id,
                            "to": to_service,
                            "error": repr(exc),
                        },
                    )

            threading.Thread(target=process_prompt_submission, daemon=True).start()
            if "application/json" in content_type:
                self._json(
                    202,
                    {
                        "accepted": True,
                        "queued": True,
                        "to": requested_to_service,
                        "provider": provider_override or "auto",
                        "username": username,
                        "session_id": session_id,
                        "session_id": session_id,
                        "service_id": self_service["service_id"],
                        "dispatch_error": None,
                    },
                )
                return
            self._redirect(f"/?{urlencode({'session_id': session_id})}")

        def log_message(self, format: str, *args: Any) -> None:
            return

    def _overview_cache_warmer() -> None:
        # Keep the overview cache warm so GET /overview returns quickly.
        # Recomputes every 3.5 s (just under the 5 s client poll interval).
        while not stopped.wait(timeout=3.5):
            try:
                _get_overview_cached(viewer_username="*", include_all=True)
            except Exception:
                pass

    def _user_response_wait_watcher() -> None:
        while not stopped.wait(timeout=3.5):
            try:
                sessions = list_all_sessions_with_users(runtime_root)
            except Exception:
                continue
            for talk in sessions:
                if not isinstance(talk, dict):
                    continue
                username = str(talk.get("username") or "").strip()
                session_id = str(talk.get("session_id") or "").strip()
                if not username or not session_id:
                    continue
                due_wait = consume_session_due_user_response_wait(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                )
                if not isinstance(due_wait, dict):
                    continue
                preferred_provider = (
                    str(due_wait.get("preferred_provider", default_provider)).strip().lower()
                    or default_provider
                )
                current_codex_service_pool, current_claude_service_pool, _current_llm_service_kinds = (
                    current_llm_service_topology()
                )
                pool_service_ids = current_claude_service_pool if preferred_provider == "claude" else current_codex_service_pool
                target_service_id = get_session_service(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                )
                if not target_service_id and pool_service_ids:
                    target_service_id = lease_session_service(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                        pool_service_ids=pool_service_ids,
                    )
                append_pending_input(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    entry=make_aize_pending_input(
                        kind="goal_feedback",
                        role="system",
                        text=(
                            "<aize_goal_feedback>"
                            "<summary>User response wait expired. Resume the active goal with the available information. "
                            "Do not wait for the user any longer; continue with the best next step and state any assumptions when needed.</summary>"
                            "</aize_goal_feedback>"
                        ),
                    ),
                )
                append_history(
                    username,
                    session_id,
                    {
                        "direction": "event",
                        "ts": utc_ts(),
                        "service_id": self_service["service_id"],
                        "event_type": "service.user_response_wait_timed_out",
                        "text": "User response wait timed out; autonomous work resumed.",
                        "event": {
                            "type": "service.user_response_wait_timed_out",
                            "dispatch_service_id": target_service_id or "",
                        },
                    },
                )
                if isinstance(target_service_id, str) and target_service_id:
                    send_router_control(
                        make_dispatch_pending_message(
                            manifest=manifest,
                            from_service_id=self_service["service_id"],
                            to_service_id=target_service_id,
                            process_id=process_id,
                            run_id=f"user-wait-timeout-{int(time.time())}",
                            username=username,
                            session_id=session_id,
                            auth_context=None,
                            reason="user_response_wait_timeout",
                        )
                    )
                write_jsonl(
                    log_path,
                    {
                        "type": "service.user_response_wait_timeout_processed",
                        "ts": utc_ts(),
                        "service_id": self_service["service_id"],
                        "process_id": process_id,
                        "username": username,
                        "session_id": session_id,
                        "dispatch_service_id": target_service_id,
                    },
                )

    threading.Thread(target=_overview_cache_warmer, daemon=True).start()
    threading.Thread(target=_user_response_wait_watcher, daemon=True).start()

    return Handler
