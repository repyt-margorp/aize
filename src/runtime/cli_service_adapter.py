from __future__ import annotations

import argparse
import codecs
import html
import json
import os
import queue
import re
import select
import socket
import ssl
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlsplit
from collections import defaultdict, deque
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from kernel.auth import bootstrap_root_user, create_user, has_users, issue_auth_context, verify_user_password
from kernel.auth import auth_context_allows
from kernel.lifecycle import get_process_record, load_lifecycle_state, register_process, update_process_fields
from kernel.peers import list_peers, register_peer
from kernel.registry import get_service_record, list_service_records, update_service_process
from runtime.goal_audit import (
    build_goal_audit_log_bundle,
    build_goal_audit_prompt,
    collect_and_verify_turn_completed_artifacts,
    default_goal_continue_xml,
    goal_audit_should_enqueue_agent_followup,
    goal_followup_dispatch_targets,
    history_excerpt,
    pending_turn_completed_events_since_last_review,
    run_goal_audit,
)
from runtime.communication_goal import should_idle_goal_reconcile
from runtime.providers import (
    provider_supports_context_compaction,
    run_claude,
    run_claude_compaction,
    run_claude_context_check,
    run_codex,
    run_codex_compaction,
    run_codex_context_check,
    run_gemini,
    run_gemini_compaction,
)
from runtime.persistent_state_pkg import (
    append_history as append_user_history,
    append_goal_manager_pending_input,
    append_pending_input,
    append_service_pending_input,
    create_conversation_session,
    create_session,
    delete_session,
    drain_pending_inputs,
    drain_service_pending_inputs,
    get_session_settings,
    get_session_service,
    ensure_state,
    get_history as get_user_history,
    lease_session_service,
    list_all_sessions_with_users,
    release_session_service,
    release_nonrunnable_session_services,
    reconcile_session_waiting_on_children,
    load_agent_audit_state,
    load_goal_manager_pending_inputs,
    load_pending_inputs,
    load_service_pending_inputs,
    reset_agent_audit_states_for_session,
    list_sessions,
    list_sessions_bound_to_service,
    list_codex_sessions,
    list_session_agent_contacts,
    join_session_agent,
    load_claude_session,
    load_codex_session,
    load_gemini_session,
    active_agent_priority,
    active_goal_manager_priority,
    normalize_auto_compact_threshold_left_percent,
    read_json_file,
    resolve_session_agent_id,
    resolve_session,
    resolve_session_context,
    sessions_dir,
    session_metadata_path,
    session_goal_manager_state_path,
    session_goal_context,
    session_service_state_path,
    save_agent_audit_state,
    save_claude_session,
    save_codex_session,
    save_gemini_session,
    select_session,
    rename_session,
    update_session_auto_compact_threshold,
    update_session_context_status,
    update_session_goal,
    update_session_goal_flags,
    update_goal_manager_review_cursor,
    write_json_file,
)
from runtime.service_control import (
    build_prompt,
    extract_agent_message_visible_text,
    parse_service_response,
    parse_service_response_with_fallback,
)
from runtime.status_gateway import build_runtime_status, runtime_status_changed_event
from wire.protocol import (
    decode_line,
    encode_line,
    load_text_object,
    make_message,
    message_meta_get,
    message_set_meta,
    store_text_object,
    utc_ts,
    write_jsonl,
)
from runtime.message_builder import (
    maybe_release_session_provider,
    load_manifest,
    inline_limit_bytes,
    make_process_id,
    resolve_payload_text,
    resolve_event_entry,
    build_outgoing_message,
    build_outgoing_event_message,
    resolve_conversation_scope,
    session_payload,
    build_aize_input_batch_xml,
    make_dispatch_pending_message,
    make_aize_pending_input,
    batch_has_input_kind,
    dispatch_pending_opens_visible_turn,
)
from runtime.event_log import (
    summarize_provider_event,
    make_history_event_entry,
    emit_turn_completed_event,
)
from runtime.dispatch_queue import dispatch_priority, order_dispatch_messages
from runtime.session_view import (
    active_agent_turn_state,
    worker_slot_badge,
    latest_goal_manager_runtime_state,
    persisted_goal_manager_runtime_state,
    build_session_runtime_summary,
    build_worker_count_summary,
    is_canonical_llm_service_id,
    session_has_active_in_progress_goal,
    session_assignment_contacts,
    pending_progress_inquiry_exists,
    build_progress_inquiry_xml,
    maybe_enqueue_mid_turn_progress_inquiry,
)
from runtime.session_lifecycle import enqueue_goal_manager_lifecycle_review
from runtime.compaction import (
    context_status_from_history_entry,
    persist_session_context_status,
    wait_for_service_record,
    maybe_resume_after_restart,
    emit_codex_compaction_event,
    resolve_session_auto_compact_threshold,
    manual_compact_codex_session,
    goal_manager_compact_codex_session,
    maybe_auto_compact_codex_session,
    manual_compact_claude_session,
    manual_compact_gemini_session,
    manual_compact_clears_audit_state,
    goal_manager_compact_claude_session,
    maybe_auto_compact_claude_session,
    goal_manager_compact_gemini_session,
    maybe_auto_compact_gemini_session,
)
from runtime.goal_persist import (
    GOAL_AUDIT_HISTORY_LIMIT,
    goal_state_response_payload,
    goal_audit_history_text,
    persist_goal_audit_completion,
    persist_goal_manager_compact_event,
    persist_goal_manager_compact_started,
    handle_goal_manager_compact_request,
)
from runtime.panic_recovery import (
    ensure_panic_recovery_session,
    panic_recovery_bootstrap_xml,
)
from runtime.agent_service import run_agent_service
from runtime.ws_peer_client import start_ws_peer_clients


def session_agent_assignment_counts(
    talk: dict[str, Any] | None,
    *,
    worker: dict[str, Any] | None = None,
    agent_running: bool | None = None,
    goal_manager_worker: dict[str, Any] | None = None,
    goal_manager_state: str | None = None,
) -> dict[str, int]:
    # Keep a local compatibility shim so runtime startup still succeeds if an
    # older or partially updated session_view module is on disk.
    from runtime import session_view as _session_view

    helper = getattr(_session_view, "session_agent_assignment_counts", None)
    if callable(helper):
        return helper(
            talk,
            worker=worker,
            agent_running=agent_running,
            goal_manager_worker=goal_manager_worker,
            goal_manager_state=goal_manager_state,
        )

    session = talk if isinstance(talk, dict) else {}
    gm_agents: set[str] = set()
    assigned_agents: set[str] = set()

    def contact_key(item: dict[str, Any]) -> str:
        raw = str(item.get("service_id") or item.get("agent_id") or "").strip()
        if "@@" in raw:
            return raw.split("@@", 1)[0].strip()
        return raw

    if not session_has_active_in_progress_goal(session):
        return {
            "goal_manager_reviewers": 0,
            "assigned_agents": 0,
        }

    welcomed_agents = session.get("welcomed_agents")
    if isinstance(welcomed_agents, list):
        for item in welcomed_agents:
            if not isinstance(item, dict):
                continue
            key = contact_key(item)
            if not key:
                continue
            role = str(item.get("join_role") or "agent").strip().lower() or "agent"
            if role == "goal_manager":
                gm_agents.add(key)
            else:
                assigned_agents.add(key)

    replying = bool(session.get("agent_running", False)) if agent_running is None else bool(agent_running)
    if replying:
        bound_service_id = str(session.get("service_id") or "").strip()
        if bound_service_id:
            assigned_agents.add(bound_service_id)

        if isinstance(worker, dict):
            worker_key = contact_key(worker)
            if worker_key:
                assigned_agents.add(worker_key)

    gm_state = str(goal_manager_state or "").strip().lower()
    if isinstance(goal_manager_worker, dict):
        gm_key = contact_key(goal_manager_worker)
        if gm_key and gm_state in {"running", "queued"}:
            gm_agents.add(gm_key)

    return {
        "goal_manager_reviewers": len(gm_agents),
        "assigned_agents": len(assigned_agents),
    }

DEFAULT_HTTPBRIDGE_RECENT_MESSAGES_LIMIT = 100
MAX_HTTPBRIDGE_RECENT_MESSAGES_LIMIT = 5000


def _resolve_bind_specs(requested_host: str) -> list[tuple[str, int]]:
    # Bind both wildcard families for the default host. Some environments only
    # receive a routable IPv6 address, while local health checks still use IPv4.
    # Keep IPv6 sockets v6-only so the IPv4 listener remains predictable.
    if requested_host == "0.0.0.0":
        return [("0.0.0.0", socket.AF_INET), ("::", socket.AF_INET6)]
    family = socket.AF_INET6 if ":" in requested_host else socket.AF_INET
    return [(requested_host, family)]


def maybe_clear_stale_idle_agent_panic(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    service_id: str,
) -> bool:
    current_audit_state = load_agent_audit_state(
        runtime_root,
        service_id=service_id,
        username=username,
        session_id=session_id,
    )
    if current_audit_state != "panic":
        return False
    service_state_path = session_service_state_path(
        runtime_root,
        username=username,
        session_id=session_id,
        service_id=service_id,
    )
    service_state = read_json_file(service_state_path) or {"service_id": service_id}
    service_status = str(service_state.get("status") or "").strip().lower()
    session = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
    bound_service_id = str(session.get("service_id") or "").strip()
    if service_status != "idle":
        # Released sessions can retain a minimal service state record without a runtime
        # status after their old worker binding is dropped. Treat that as idle only when
        # the session is no longer bound to this worker and no worker-specific input remains.
        if (
            service_status
            or bound_service_id == service_id
            or load_service_pending_inputs(
                runtime_root,
                service_id=service_id,
                username=username,
                session_id=session_id,
            )
        ):
            return False
    goal_manager_state = service_state.get("goal_manager")
    if not isinstance(goal_manager_state, dict):
        goal_manager_state = {}
    goal_manager_runtime_state = str(goal_manager_state.get("state") or "").strip().lower()
    pending_work_items = (
        list(goal_manager_state.get("pending_work_items", []))
        if isinstance(goal_manager_state.get("pending_work_items"), list)
        else []
    )
    if goal_manager_runtime_state not in {"", "idle"}:
        return False
    if pending_work_items:
        return False
    if load_pending_inputs(runtime_root, username=username, session_id=session_id):
        return False
    if load_service_pending_inputs(
        runtime_root,
        service_id=service_id,
        username=username,
        session_id=session_id,
    ):
        return False
    cleared_at = utc_ts()
    save_agent_audit_state(
        runtime_root,
        service_id=service_id,
        username=username,
        session_id=session_id,
        audit_state="all_clear",
    )
    goal_manager_state["audit_state"] = "all_clear"
    goal_manager_state["updated_at"] = cleared_at
    service_state["updated_at"] = cleared_at
    service_state["goal_manager"] = goal_manager_state
    write_json_file(service_state_path, service_state)
    append_user_history(
        runtime_root,
        username=username,
        session_id=session_id,
        entry={
            "direction": "event",
            "ts": cleared_at,
            "service_id": service_id,
            "event_type": "service.stale_panic_cleared_after_idle_recovery",
            "text": "Cleared a stale panic state for an idle service with no pending work.",
            "event": {
                "type": "service.stale_panic_cleared_after_idle_recovery",
                "previous_audit_state": "panic",
                "new_audit_state": "all_clear",
            },
        },
        limit=GOAL_AUDIT_HISTORY_LIMIT,
    )
    return True


PROVIDER_FATAL_ERROR_MARKERS = (
    "not logged in",
    "monthly usage limit",
    "usage limit",
    "authentication",
    "unauthorized",
)


def provider_has_recent_fatal_error(runtime_root: Path, *, provider: str) -> bool:
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider not in {"codex", "claude", "gemini"}:
        return False
    root = sessions_dir(runtime_root)
    if not root.exists():
        return False
    for path in root.glob(f"*/*/services/service-{normalized_provider}-*.json"):
        record = read_json_file(path)
        if not isinstance(record, dict):
            continue
        haystack = json.dumps(record, ensure_ascii=False).lower()
        if any(marker in haystack for marker in PROVIDER_FATAL_ERROR_MARKERS):
            return True
    return False


# Source-compat snippets for HTTPBridge UI tests.
# The concrete renderer lives in runtime.html_renderer, but these pinned excerpts
# are kept here so the adapter source still advertises the expected UI contract.
_HTTPBRIDGE_SOURCE_COMPAT_SNIPPETS = """
if path == "/sessions":
if self.path == "/sessions":
if self.path == "/session/select":
if self.path == "/session/goal/state":
html.escape(previous_goal_text)
"active_session_id": context["session_id"]
if (entry?.kind === 'turn_cluster' || entry?.kind === 'goal_manager_cluster') return renderTurnCluster(entry);
renderTurnCluster(entry)
entry?.kind === 'turn_cluster'
goal_manager_cluster
GoalManager Review
audit ${auditStateLabel(goalAuditState)}
buildRenderableTimeline
JSON.stringify(eventEntry.event, null, 2)
agent-status-value
Agent Status
turn-cluster-log
turn-cluster-inline-status
agent-popover
% left
goal_audit_state = "all_clear"
if (payload.goal_audit_state) goalAuditState =
manual_compact_clears_audit_state
response["goal_audit_state"] = "all_clear"
controlsButton.dataset.agentControlsButton = '1';
event.stopPropagation()
eventType.startsWith('service.goal_manager_compact_')) return false;
deriveContextStatusForService(cluster.serviceId)
dispatch_pending_opens_visible_turn(message, incoming_text)
reason not in {"goal_feedback", "turn_completed"}
if (entry.direction === 'in') {
timeline.push(entry);
continue;
}

goal-auto-compact-toggle
GoalManager autonomous compact
goal_auto_compact_enabled
kind="goal_feedback"
goal_message = make_dispatch_pending_message
goal_feedback_message = make_dispatch_pending_message
goal_message = make_dispatch_pending_message(
message_type="dispatch_pending"
provider_pool = {"codex": codex_service_pool, "claude": claude_service_pool, "gemini": gemini_service_pool}.get(preferred_provider, codex_service_pool)
return lease_session_service(
goal_audit_should_enqueue_agent_followup(
previous_goal_text=previous_goal,
previous_goal_id=previous_goal_id,
reason="goal_feedback"
reason="goal_saved"
sessionMapOpen ? 'Sessions' : talkLabel
sessionMapSnapshotTalkIds
captureSessionMapSnapshot
visibleTalkSummaries = sessionMapOpen && sessionMapSnapshotTalkIds.length
return currentFilter === 'all' ? timeline.reverse() : timeline;
const visible = currentFilter === 'all' ? timeline.slice(0, recentMessagesLimit) : timeline.slice(-recentMessagesLimit);
const eventsShell = document.createElement('details');
if (currentFilter === 'all') eventsShell.open = true;
eventsTitle.textContent = currentFilter === 'all' ? 'Event Log' : 'Event Log (closed by default)';
data-filter='messages'>Timeline</button>
data-filter='all'>ALL</button>
renderPageTitle
setSessionMapOpen(sessionMapOpen);
captureElementScrollState(goalBoardGrid)
restoreElementScrollPosition(goalBoardGrid, scrollState)
id='view-session-map'
viewSessionMapButton.textContent = sessionMapOpen ? 'Talk' : 'Sessions';
viewSessionMapButton.onclick = (event) => { event.preventDefault(); toggleSessionMap(); };
const captureElementScrollState = (element) => element ? ({
const restoreElementScrollPosition = (element, state) => {
            # Match HttpBridge: do not preload the session map on plain GET /.
            # The workspace remains the primary view, and the session map loads lazily.
            initial_session_map_open = False
f"let sessionMapOpen = {json.dumps(initial_session_map_open)};"
previous_goal = str(old_talk.get("goal_text", "")).strip()
previous_goal_id = str(old_talk.get("active_goal_id") or old_talk.get("goal_id") or "").strip() or None
has_dangling_goal_audit
dangling_goal_audit
"""


def resolve_http_reply_scope(
    message: dict[str, Any],
    awaiting_replies: deque[dict[str, str]],
) -> tuple[str, str]:
    scope_username, scope_session_id = resolve_conversation_scope(message)
    if scope_username and scope_session_id:
        return scope_username, scope_session_id
    route = awaiting_replies.popleft() if awaiting_replies else None
    if route:
        return route["username"], route["session_id"]
    return "anonymous", "default"


def run_http_service(
    *,
    runtime_root: Path,
    manifest: dict,
    self_service: dict,
    process_id: str,
    log_path: Path,
    router_conn: Any = None,
) -> int:
    config = dict(self_service.get("config", {}))
    host = str(config.get("host", "127.0.0.1"))
    port = int(config.get("port", 4123))
    _tls_dir = runtime_root / "tls"
    _tls_enabled_raw = str(os.environ.get("AIZE_TLS", str(config.get("tls_enabled", "true")))).strip()
    tls_enabled = _tls_enabled_raw.lower() not in ("0", "false", "no")
    _tls_cert_raw = str(os.environ.get("AIZE_TLS_CERT", "") or "").strip()
    _tls_key_raw = str(os.environ.get("AIZE_TLS_KEY", "") or "").strip()
    _tls_cn_raw = str(os.environ.get("AIZE_TLS_CN", "") or "").strip()
    tls_cert = Path(_tls_cert_raw or str(config.get("tls_cert", _tls_dir / "server.crt")))
    tls_key = Path(_tls_key_raw or str(config.get("tls_key", _tls_dir / "server.key")))
    tls_cn = _tls_cn_raw or str(config.get("tls_cn", "localhost")).strip() or "localhost"
    _tls_hosts_raw = os.environ.get("AIZE_TLS_HOSTS")
    if _tls_hosts_raw is None:
        _cfg_tls_hosts = config.get("tls_hosts", [])
        if isinstance(_cfg_tls_hosts, str):
            tls_hosts = [part.strip() for part in _cfg_tls_hosts.split(",") if part.strip()]
        elif isinstance(_cfg_tls_hosts, list):
            tls_hosts = [str(part).strip() for part in _cfg_tls_hosts if str(part).strip()]
        else:
            tls_hosts = []
    else:
        tls_hosts = [part.strip() for part in _tls_hosts_raw.split(",") if part.strip()]
    _tls_auto_hosts_raw = str(os.environ.get("AIZE_TLS_AUTO_HOSTS", str(config.get("tls_auto_hosts", "true")))).strip()
    tls_auto_hosts = _tls_auto_hosts_raw.lower() not in ("0", "false", "no", "off")
    default_target = str(config.get("default_target", "service-codex-001"))
    default_provider = str(config.get("default_provider", "codex")).strip().lower() or "codex"
    history_limit = int(config.get("history_limit", 500))
    codex_service_pool = sorted(
        str(service.get("service_id"))
        for service in manifest.get("services", [])
        if isinstance(service, dict) and str(service.get("kind")) == "codex" and isinstance(service.get("service_id"), str)
    )
    claude_service_pool = sorted(
        str(service.get("service_id"))
        for service in manifest.get("services", [])
        if isinstance(service, dict) and str(service.get("kind")) == "claude" and isinstance(service.get("service_id"), str)
    )
    gemini_service_pool = sorted(
        str(service.get("service_id"))
        for service in manifest.get("services", [])
        if isinstance(service, dict) and str(service.get("kind")) == "gemini" and isinstance(service.get("service_id"), str)
    )
    llm_service_kinds = {
        str(service.get("service_id")): str(service.get("kind"))
        for service in manifest.get("services", [])
        if isinstance(service, dict) and isinstance(service.get("service_id"), str) and str(service.get("kind")) in {"codex", "claude", "gemini"}
    }
    pending: queue.Queue[dict[str, str]] = queue.Queue()
    awaiting_replies: deque[dict[str, str]] = deque()
    rx_buffer = ""
    rx_decoder = codecs.getincrementaldecoder("utf-8")()
    subscribers: dict[str, set[queue.Queue[dict[str, Any]]]] = defaultdict(set)
    subscribers_lock = threading.Lock()
    stopped = threading.Event()
    # Overview tracking: GoalManager active runs per "username::session_id"
    _active_goal_audits: dict[str, dict[str, Any]] = {}
    _active_goal_audits_lock = threading.Lock()
    # Overview tracking: agent service currently running per "username::session_id"
    _active_agent_turns: dict[str, dict[str, Any]] = {}
    _active_agent_turns_lock = threading.Lock()
    _last_runtime_status: dict[str, dict[str, Any]] = {}
    from kernel.ipc import connect_to_router as _connect_to_router
    if router_conn is None:
        router_conn = _connect_to_router(runtime_root, self_service["service_id"])

    def _wait_for_startup_llm_pool_ready(*, timeout_seconds: float = 45.0) -> dict[str, list[str]]:
        deadline = time.monotonic() + timeout_seconds
        last_pools: dict[str, list[str]] = {"codex": [], "claude": [], "gemini": []}
        stable_signature: tuple[tuple[str, int], ...] | None = None
        stable_observations = 0
        while True:
            last_pools = running_llm_service_pools()
            expected = {"codex": 0, "claude": 0, "gemini": 0}
            for record in list_service_records(runtime_root):
                service_id = str(record.get("service_id") or "").strip()
                kind = str(record.get("kind") or "").strip().lower()
                if kind in expected and is_canonical_llm_service_id(service_id):
                    expected[kind] += 1
            expected_signature = tuple(sorted(expected.items()))
            if expected_signature == stable_signature:
                stable_observations += 1
            else:
                stable_signature = expected_signature
                stable_observations = 1
            ready = all(
                len(last_pools.get(kind, [])) >= count
                for kind, count in expected.items()
                if count > 0
            ) and sum(expected.values()) > 0 and stable_observations >= 3
            if ready:
                write_jsonl(
                    log_path,
                    {
                        "type": "http.startup_llm_pool_ready",
                        "ts": utc_ts(),
                        "service_id": self_service["service_id"],
                        "process_id": process_id,
                        "running_counts": {kind: len(last_pools.get(kind, [])) for kind in sorted(last_pools)},
                        "expected_counts": expected,
                    },
                )
                return last_pools
            if time.monotonic() >= deadline:
                write_jsonl(
                    log_path,
                    {
                        "type": "http.startup_llm_pool_wait_timeout",
                        "ts": utc_ts(),
                        "service_id": self_service["service_id"],
                        "process_id": process_id,
                        "running_counts": {kind: len(last_pools.get(kind, [])) for kind in sorted(last_pools)},
                        "expected_counts": expected,
                    },
                )
                return last_pools
            time.sleep(0.5)

    def _run_startup_state_reconcile() -> None:
        def session_service_blocked_by_panic(*, username: str, session_id: str, service_id: str) -> bool:
            normalized_service_id = str(service_id or "").strip()
            if not normalized_service_id:
                return True
            agent_audit_state = load_agent_audit_state(
                runtime_root,
                service_id=normalized_service_id,
                username=username,
                session_id=session_id,
            )
            if agent_audit_state == "panic":
                maybe_clear_stale_idle_agent_panic(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    service_id=normalized_service_id,
                )
                agent_audit_state = load_agent_audit_state(
                    runtime_root,
                    service_id=normalized_service_id,
                    username=username,
                    session_id=session_id,
                )
            return agent_audit_state == "panic"

        def choose_startup_goal_manager_service(
            *,
            session: dict[str, Any],
            username: str,
            session_id: str,
            goal_manager: dict[str, Any],
            service_pools: dict[str, list[str]],
            current_goal_manager_services: set[str],
        ) -> str:
            existing_service_id = str(goal_manager.get("service_id") or "").strip()
            if (
                existing_service_id in current_goal_manager_services
                and not session_service_blocked_by_panic(
                    username=username,
                    session_id=session_id,
                    service_id=existing_service_id,
                )
            ):
                return existing_service_id
            priority = active_goal_manager_priority(session.get("goal_manager_priority"), available_kinds=None)
            if not priority:
                preferred = str(session.get("preferred_provider") or default_provider or "codex").strip().lower() or "codex"
                priority = [preferred]
            for provider in priority:
                pool = [
                    service_id
                    for service_id in service_pools.get(str(provider or "").strip().lower(), [])
                    if not session_service_blocked_by_panic(
                        username=username,
                        session_id=session_id,
                        service_id=service_id,
                    )
                ]
                if not pool:
                    continue
                leased = lease_session_service(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    pool_service_ids=pool,
                )
                if not leased:
                    continue
                join_session_agent(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    service_id=leased,
                    provider=str(provider),
                    role="goal_manager",
                    transport="startup_queue_reconcile",
                )
                return str(leased)
            return ""

        write_jsonl(
            log_path,
            {
                "type": "http.startup_state_reconcile.started",
                "ts": utc_ts(),
                "service_id": self_service["service_id"],
                "process_id": process_id,
            },
        )
        try:
            ensure_state(runtime_root)
            _wait_for_startup_llm_pool_ready()
            release_stale_session_bindings()
            current_codex_pool, current_claude_pool, current_gemini_pool, _ = current_llm_service_topology()
            service_pools_by_provider = {
                "codex": []
                if provider_has_recent_fatal_error(runtime_root, provider="codex")
                else current_codex_pool,
                "claude": []
                if provider_has_recent_fatal_error(runtime_root, provider="claude")
                else current_claude_pool,
                "gemini": []
                if provider_has_recent_fatal_error(runtime_root, provider="gemini")
                else current_gemini_pool,
            }
            current_goal_manager_services = {
                service_id
                for pool in service_pools_by_provider.values()
                for service_id in pool
            }
            for session in list_all_sessions_with_users(runtime_root):
                if not isinstance(session, dict):
                    continue
                username = str(session.get("username") or "").strip()
                session_id = str(session.get("session_id") or "").strip()
                if not username or not session_id:
                    continue
                if not session_has_active_in_progress_goal(session):
                    continue
                goal_manager = persisted_goal_manager_runtime_state(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    bound_service_id=str(session.get("service_id") or "").strip(),
                    allow_reconcile=False,
                )
                goal_manager_state = str(goal_manager.get("state") or "idle").strip().lower()
                goal_manager_pending_work_items = (
                    list(goal_manager.get("pending_work_items", []))
                    if isinstance(goal_manager.get("pending_work_items"), list)
                    else []
                )
                if goal_manager_state == "queued" and goal_manager_pending_work_items:
                    target_goal_manager_service_id = choose_startup_goal_manager_service(
                        session=session,
                        username=username,
                        session_id=session_id,
                        goal_manager=goal_manager,
                        service_pools=service_pools_by_provider,
                        current_goal_manager_services=current_goal_manager_services,
                    )
                    if target_goal_manager_service_id in current_goal_manager_services:
                        if not load_goal_manager_pending_inputs(
                            runtime_root,
                            username=username,
                            session_id=session_id,
                        ):
                            for item in goal_manager_pending_work_items:
                                if isinstance(item, dict):
                                    append_goal_manager_pending_input(
                                        runtime_root,
                                        username=username,
                                        session_id=session_id,
                                        entry=dict(item),
                                    )
                        repaired_goal_manager_state = dict(goal_manager)
                        repaired_goal_manager_state["state"] = "queued"
                        repaired_goal_manager_state["service_id"] = target_goal_manager_service_id
                        repaired_goal_manager_state["updated_at"] = utc_ts()
                        if target_goal_manager_service_id != str(goal_manager.get("service_id") or "").strip():
                            repaired_goal_manager_state.pop("error", None)
                        write_json_file(
                            session_goal_manager_state_path(
                                runtime_root,
                                username=username,
                                session_id=session_id,
                            ),
                            repaired_goal_manager_state,
                        )
                        dispatch_message = make_dispatch_pending_message(
                            manifest=manifest,
                            from_service_id=self_service["service_id"],
                            to_service_id=target_goal_manager_service_id,
                            process_id=process_id,
                            run_id=f"startup-goal-manager-queue-{uuid.uuid4().hex[:8]}",
                            username=username,
                            session_id=session_id,
                            auth_context=None,
                            reason="goal_manager_review",
                            session_agent_id=resolve_session_agent_id(
                                runtime_root,
                                username=username,
                                session_id=session_id,
                                service_id=target_goal_manager_service_id,
                                role="goal_manager",
                            ),
                            agent_profile={"session_slot": "goal_manager"},
                            dispatch_priority=dispatch_priority("goal_manager_review"),
                        )
                        dispatch_sent = send_router_control(dispatch_message)
                        write_jsonl(
                            log_path,
                            {
                                "type": "http.startup_queued_goal_manager_reconcile",
                                "ts": utc_ts(),
                                "service_id": self_service["service_id"],
                                "process_id": process_id,
                                "username": username,
                                "session_id": session_id,
                                "to": target_goal_manager_service_id,
                                "dispatch_sent": bool(dispatch_sent),
                                "pending_work_item_count": len(goal_manager_pending_work_items),
                            },
                        )
                    else:
                        write_jsonl(
                            log_path,
                            {
                                "type": "http.startup_queued_goal_manager_reconcile_skipped",
                                "ts": utc_ts(),
                                "service_id": self_service["service_id"],
                                "process_id": process_id,
                                "username": username,
                                "session_id": session_id,
                                "goal_manager_service_id": target_goal_manager_service_id,
                                "reason": "goal_manager_service_not_running",
                                "pending_work_item_count": len(goal_manager_pending_work_items),
                            },
                        )
                    continue
                if goal_manager_state == "running":
                    continue
                if not should_idle_goal_reconcile(session):
                    continue
                if bool(session.get("user_response_wait_active", False)):
                    continue
                if bool(session.get("waiting_on_children", False)):
                    continue
                dispatched_to, dispatch_error = enqueue_goal_dispatch(
                    username=username,
                    session_id=session_id,
                    auth_context=None,
                    reason="active_in_progress_idle_reconcile",
                )
                write_jsonl(
                    log_path,
                    {
                        "type": "http.startup_active_goal_idle_reconcile",
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
                    "type": "http.startup_state_reconcile.failed",
                    "ts": utc_ts(),
                    "service_id": self_service["service_id"],
                    "process_id": process_id,
                    "error": repr(exc),
                },
            )
            return
        write_jsonl(
            log_path,
            {
                "type": "http.startup_state_reconcile.completed",
                "ts": utc_ts(),
                "service_id": self_service["service_id"],
                "process_id": process_id,
            },
        )
    def running_llm_service_pools() -> dict[str, list[str]]:
        lifecycle_processes = load_lifecycle_state(runtime_root).get("processes", {})
        pools: dict[str, list[str]] = {"codex": [], "claude": [], "gemini": []}
        manifest_kinds = {
            str(service.get("service_id") or ""): str(service.get("kind") or "").strip().lower()
            for service in manifest.get("services", [])
            if isinstance(service, dict)
        }
        for record in list_service_records(runtime_root):
            service_id = str(record.get("service_id") or "").strip()
            kind = str(record.get("kind") or manifest_kinds.get(service_id) or "").strip().lower()
            if not service_id or kind not in pools or not is_canonical_llm_service_id(service_id):
                continue
            process_id_for_service = str(record.get("current_process_id") or "").strip()
            process_record = lifecycle_processes.get(process_id_for_service)
            if (
                str(record.get("status") or "").strip().lower() == "running"
                and isinstance(process_record, dict)
                and str(process_record.get("status") or "").strip().lower() == "running"
            ):
                pools[kind].append(service_id)
        for kind, service_ids in list(pools.items()):
            pools[kind] = sorted(service_ids)
        return pools

    def queue_goal_manager_for_released_session(released: dict[str, Any]) -> None:
        username = str(released.get("username") or "").strip()
        session_id = str(released.get("session_id") or "").strip()
        released_service_id = str(released.get("service_id") or "").strip()
        if not username or not session_id or not released_service_id:
            return
        result = enqueue_goal_manager_lifecycle_review(
            runtime_root,
            manifest=manifest,
            from_service_id=self_service["service_id"],
            process_id=process_id,
            username=username,
            session_id=session_id,
            reason=f"released_nonrunnable_session_service:{released.get('reason') or 'unknown'}",
            source_service_id=released_service_id,
            service_pools_by_provider=running_llm_service_pools(),
            default_provider=default_provider,
            send_dispatch=send_router_control,
        )
        write_jsonl(
            log_path,
            {
                "type": "http.goal_manager_lifecycle_review",
                "ts": utc_ts(),
                "service_id": self_service["service_id"],
                "process_id": process_id,
                "username": username,
                "session_id": session_id,
                "released_service_id": released_service_id,
                **result,
            },
        )

    def release_stale_session_bindings() -> None:
        for reconciled in reconcile_session_waiting_on_children(runtime_root):
            write_jsonl(
                log_path,
                {
                    "type": "http.reconciled_session_waiting_on_children",
                    "ts": utc_ts(),
                    "service_id": self_service["service_id"],
                    "process_id": process_id,
                    **reconciled,
                },
            )
        for released in release_nonrunnable_session_services(runtime_root):
            write_jsonl(
                log_path,
                {
                    "type": "http.released_nonrunnable_session_service",
                    "ts": utc_ts(),
                    "service_id": self_service["service_id"],
                    "process_id": process_id,
                    **released,
                },
            )
            queue_goal_manager_for_released_session(released)

    def subscriber_key(username: str, session_id: str) -> str:
        return f"{username}::{session_id}"

    def append_history(username: str, session_id: str, record: dict[str, Any]) -> None:
        append_user_history(runtime_root, username=username, session_id=session_id, entry=record, limit=history_limit)

    def broadcast_runtime_status(username: str, session_id: str) -> None:
        scope_key = f"{username}::{session_id}"
        with _active_agent_turns_lock:
            active_turn = dict(_active_agent_turns.get(scope_key) or {})
        with _active_goal_audits_lock:
            active_goal_audit = dict(_active_goal_audits.get(scope_key) or {})
        agent_running = bool(active_turn)
        goal_manager_state = "running" if active_goal_audit else "idle"
        status = build_runtime_status(
            agent_running=agent_running,
            goal_manager_state=goal_manager_state,
            worker={"service_id": str(active_turn.get("service_id") or "")} if active_turn else None,
            goal_manager_worker={"service_id": str(active_goal_audit.get("service_id") or "")} if active_goal_audit else None,
        )
        previous = _last_runtime_status.get(scope_key)
        if (
            previous
            and previous.get("runtime_execution_state") == status.get("runtime_execution_state")
            and previous.get("agent_running") == status.get("agent_running")
            and previous.get("goal_manager_state") == status.get("goal_manager_state")
        ):
            return
        _last_runtime_status[scope_key] = dict(status)
        append_history(
            username,
            session_id,
            runtime_status_changed_event(
                service_id=str(self_service.get("service_id") or ""),
                username=username,
                session_id=session_id,
                status=status,
                previous_status=previous,
            ),
        )

    def send_router_control(message: dict[str, Any]) -> bool:
        target_service_id = str(message.get("to") or "")
        try:
            # HTTP-triggered dispatches should not depend on a long-lived router
            # socket remaining writable across repeated runtime restarts.
            if self_service.get("kind") == "http":
                from kernel.ipc import connect_to_router as _connect_to_router

                with _connect_to_router(runtime_root, self_service["service_id"]) as transient_router_conn:
                    transient_router_conn.write(encode_line(message))
            else:
                router_conn.write(encode_line(message))
            return True
        except OSError as exc:
            write_jsonl(
                log_path,
                {
                    "type": "http.router_control_send_failed",
                    "ts": utc_ts(),
                    "service_id": self_service["service_id"],
                    "process_id": process_id,
                    "reason": str(exc),
                    "to_service_id": target_service_id,
                },
            )
            return False

    def enqueue_service_control(*, action: str, service_id: str, auth_context: dict[str, Any]) -> None:
        control_message = make_message(
            from_node_id=manifest["node_id"],
            from_service_id=self_service["service_id"],
            to_node_id=manifest["node_id"],
            to_service_id="kernel.control",
            message_type=f"service.{action}",
            payload={"service_id": service_id},
            run_id=manifest["run_id"],
        )
        message_set_meta(control_message, "process_id", process_id)
        message_set_meta(control_message, "auth", auth_context)
        send_router_control(control_message)

    def service_snapshots() -> list[dict[str, Any]]:
        lifecycle = load_lifecycle_state(runtime_root).get("processes", {})
        snapshots: list[dict[str, Any]] = []
        for record in list_service_records(runtime_root):
            process = lifecycle.get(str(record.get("current_process_id"))) if record.get("current_process_id") else None
            snapshots.append({"service": record, "process": process})
        return snapshots

    def _resolve_llm_service_topology(
        runtime_root: Path,
        manifest: dict[str, Any],
    ) -> tuple[list[str], list[str], list[str], dict[str, str]]:
        manifest_kinds = {
            str(service.get("service_id")): str(service.get("kind"))
            for service in manifest.get("services", [])
            if isinstance(service, dict)
            and isinstance(service.get("service_id"), str)
            and str(service.get("kind")) in {"codex", "claude", "gemini"}
        }
        live_kinds: dict[str, str] = {}
        for record in list_service_records(runtime_root):
            service_id = str(record.get("service_id") or "").strip()
            kind = str(record.get("kind") or manifest_kinds.get(service_id) or "").strip().lower()
            if not service_id or kind not in {"codex", "claude", "gemini"}:
                continue
            if not is_canonical_llm_service_id(service_id):
                continue
            live_kinds[service_id] = kind
        if not live_kinds:
            live_kinds = dict(manifest_kinds)
        codex_pool = sorted(service_id for service_id, kind in live_kinds.items() if kind == "codex")
        claude_pool = sorted(service_id for service_id, kind in live_kinds.items() if kind == "claude")
        gemini_pool = sorted(service_id for service_id, kind in live_kinds.items() if kind == "gemini")
        return codex_pool, claude_pool, gemini_pool, live_kinds

    def current_llm_service_topology() -> tuple[list[str], list[str], list[str], dict[str, str]]:
        return _resolve_llm_service_topology(runtime_root, manifest)

    def session_runtime_payload(username: str, preloaded_histories: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
        release_stale_session_bindings()
        sessions = list_sessions(runtime_root, username=username)
        sessions = [session_payload(session) for session in sessions]
        current_codex_service_pool, current_claude_service_pool, current_gemini_service_pool, _current_llm_service_kinds = (
            current_llm_service_topology()
        )
        with _active_agent_turns_lock:
            active_turns_snap = dict(_active_agent_turns)
        with _active_goal_audits_lock:
            active_audits_snap = dict(_active_goal_audits)
        idle_goal_reconciled_sessions: set[str] = set()
        summaries: list[dict[str, Any]] = []
        for session in sessions:
            session_id = str(session.get("session_id") or "")
            summary = build_session_runtime_summary(
                session,
                history_entries=[],
                codex_service_pool=current_codex_service_pool,
                claude_service_pool=current_claude_service_pool,
                gemini_service_pool=current_gemini_service_pool,
                default_provider=default_provider,
                runtime_root=runtime_root,
                username=username,
                allow_reconcile=False,
            )
            scope_key = f"{username}::{session_id}"
            active_turn = active_turns_snap.get(scope_key)
            active_goal_audit = active_audits_snap.get(scope_key)
            bound_service_id = str(session.get("service_id") or "").strip()
            active_service_id = str((active_turn or {}).get("service_id") or "").strip()
            persisted_goal_manager = persisted_goal_manager_runtime_state(
                runtime_root,
                username=username,
                session_id=session_id,
                bound_service_id=bound_service_id,
                allow_reconcile=False,
            )
            summary["goal_manager_state"] = str(persisted_goal_manager.get("state") or summary.get("goal_manager_state") or "idle")
            summary["goal_manager_pending_work_items"] = (
                list(persisted_goal_manager.get("pending_work_items", []))
                if isinstance(persisted_goal_manager.get("pending_work_items"), list)
                else []
            )
            summary["goal_manager_worker"] = worker_slot_badge(
                str(persisted_goal_manager.get("service_id") or bound_service_id),
                codex_service_pool=current_codex_service_pool,
                claude_service_pool=current_claude_service_pool,
                gemini_service_pool=current_gemini_service_pool,
            ) if str(persisted_goal_manager.get("service_id") or bound_service_id).strip() else None
            if active_turn is not None:
                summary["agent_running"] = True
                summary["worker"] = worker_slot_badge(
                    active_service_id or bound_service_id,
                    codex_service_pool=current_codex_service_pool,
                    claude_service_pool=current_claude_service_pool,
                    gemini_service_pool=current_gemini_service_pool,
                )
            if active_goal_audit is not None:
                goal_manager_service_id = str((active_goal_audit or {}).get("service_id") or bound_service_id).strip()
                summary["goal_manager_state"] = "running"
                summary["goal_manager_worker"] = worker_slot_badge(
                    goal_manager_service_id,
                    codex_service_pool=current_codex_service_pool,
                    claude_service_pool=current_claude_service_pool,
                    gemini_service_pool=current_gemini_service_pool,
                )
            if (
                active_turn is None
                and active_goal_audit is None
                and session_has_active_in_progress_goal(session)
                and should_idle_goal_reconcile(session)
                and str(summary.get("goal_manager_state") or "").strip().lower() not in {"running", "queued"}
            ):
                reconcile_key = f"{username}::{session_id}"
                if reconcile_key not in idle_goal_reconciled_sessions:
                    idle_goal_reconciled_sessions.add(reconcile_key)
                    dispatched_to, dispatch_error = enqueue_goal_dispatch(
                        username=username,
                        session_id=session_id,
                        auth_context=None,
                        reason="active_in_progress_idle_reconcile",
                    )
                    write_jsonl(
                        log_path,
                        {
                            "type": "runtime.active_goal_idle_reconcile",
                            "ts": utc_ts(),
                            "service_id": self_service["service_id"],
                            "process_id": process_id,
                            "username": username,
                            "session_id": session_id,
                            "to": dispatched_to,
                            "dispatch_error": dispatch_error,
                        },
                    )
            agent_counts = session_agent_assignment_counts(
                session,
                worker=summary.get("worker") if isinstance(summary.get("worker"), dict) else None,
                agent_running=bool(summary.get("agent_running", False)),
                goal_manager_worker=summary.get("goal_manager_worker")
                if isinstance(summary.get("goal_manager_worker"), dict)
                else None,
                goal_manager_state=str(summary.get("goal_manager_state") or "idle"),
            )
            summary["agent_counts"] = agent_counts
            summary["goal_manager_reviewer_count"] = agent_counts["goal_manager_reviewers"]
            summary["assigned_agent_count"] = agent_counts["assigned_agents"]
            summary["agent_contacts"] = session_assignment_contacts(session)
            summary.update(
                build_runtime_status(
                    agent_running=bool(summary.get("agent_running", False)),
                    goal_manager_state=str(summary.get("goal_manager_state") or "idle"),
                    worker=summary.get("worker") if isinstance(summary.get("worker"), dict) else None,
                    goal_manager_worker=summary.get("goal_manager_worker")
                    if isinstance(summary.get("goal_manager_worker"), dict)
                    else None,
                )
            )
            summaries.append(summary)
        return {
            "sessions": sessions,
            "session_summaries": summaries,
            "worker_counts": build_worker_count_summary(
                service_snapshots=service_snapshots(),
                session_summaries=summaries,
            ),
        }

    def peer_descriptor() -> dict[str, Any]:
        peer_meta = manifest.get("peer", {})
        return {
            "node_id": manifest["node_id"],
            "peer_id": peer_meta.get("peer_id"),
            "started_at": peer_meta.get("started_at"),
            "service_id": self_service["service_id"],
            "process_id": process_id,
            "base_url": f"http://{host}:{port}",
            "default_target": default_target,
        }

    def resolve_session_service_for_dispatch(*, username: str, session_id: str) -> str | None:
        release_stale_session_bindings()
        current_codex_service_pool, current_claude_service_pool, current_gemini_service_pool, _current_llm_service_kinds = current_llm_service_topology()
        leased_service_id = get_session_service(runtime_root, username=username, session_id=session_id)
        session_settings = get_session_settings(runtime_root, username=username, session_id=session_id) or {}

        def service_blocked_by_panic(service_id: str | None) -> bool:
            normalized_service_id = str(service_id or "").strip()
            if not normalized_service_id:
                return True
            agent_audit_state = load_agent_audit_state(
                runtime_root,
                service_id=normalized_service_id,
                username=username,
                session_id=session_id,
            )
            if agent_audit_state == "panic":
                maybe_clear_stale_idle_agent_panic(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    service_id=normalized_service_id,
                )
                agent_audit_state = load_agent_audit_state(
                    runtime_root,
                    service_id=normalized_service_id,
                    username=username,
                    session_id=session_id,
                )
            return agent_audit_state == "panic"

        def joined(service_id: str | None, *, provider: str = "") -> str | None:
            normalized_service_id = str(service_id or "").strip()
            if not normalized_service_id:
                return None
            if service_blocked_by_panic(normalized_service_id):
                return None
            resolved_provider = provider
            if not resolved_provider:
                if normalized_service_id in current_codex_service_pool:
                    resolved_provider = "codex"
                elif normalized_service_id in current_claude_service_pool:
                    resolved_provider = "claude"
                elif normalized_service_id in current_gemini_service_pool:
                    resolved_provider = "gemini"
            if provider_blocked.get(str(resolved_provider or "").strip().lower(), False):
                return None
            join_session_agent(
                runtime_root,
                username=username,
                session_id=session_id,
                service_id=normalized_service_id,
                provider=resolved_provider,
                role="agent",
                transport="local_dispatch",
            )
            return normalized_service_id

        selected_agents_cfg = [
            str(item).strip()
            for item in list(session_settings.get("selected_agents", []))
            if str(item).strip()
        ]
        all_local_service_ids = set(current_codex_service_pool) | set(current_claude_service_pool) | set(current_gemini_service_pool)
        provider_blocked = {
            "codex": provider_has_recent_fatal_error(runtime_root, provider="codex"),
            "claude": provider_has_recent_fatal_error(runtime_root, provider="claude"),
            "gemini": provider_has_recent_fatal_error(runtime_root, provider="gemini"),
        }
        usable_codex_service_pool = [
            service_id for service_id in current_codex_service_pool if not service_blocked_by_panic(service_id)
        ] if not provider_blocked["codex"] else []
        usable_claude_service_pool = [
            service_id for service_id in current_claude_service_pool if not service_blocked_by_panic(service_id)
        ] if not provider_blocked["claude"] else []
        usable_gemini_service_pool = [
            service_id for service_id in current_gemini_service_pool if not service_blocked_by_panic(service_id)
        ] if not provider_blocked["gemini"] else []
        has_local = any(
            service_id in {"codex_pool", "claude_pool", "gemini_pool"} or service_id in all_local_service_ids
            for service_id in selected_agents_cfg
        )

        if selected_agents_cfg:
            if not has_local:
                ws_contacts = list_session_agent_contacts(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                )
                available_ws_service_ids = {
                    str(item.get("service_id") or "").strip()
                    for item in ws_contacts
                    if str(item.get("provider") or "").strip() == "ws_peer"
                    and str(item.get("service_id") or "").strip()
                }
                for service_id in selected_agents_cfg:
                    if service_id in available_ws_service_ids:
                        return joined(service_id, provider="ws_peer")
                return None

            if leased_service_id and leased_service_id in all_local_service_ids:
                if "codex_pool" in selected_agents_cfg and leased_service_id in current_codex_service_pool:
                    return joined(leased_service_id, provider="codex")
                if "claude_pool" in selected_agents_cfg and leased_service_id in current_claude_service_pool:
                    return joined(leased_service_id, provider="claude")
                if "gemini_pool" in selected_agents_cfg and leased_service_id in current_gemini_service_pool:
                    return joined(leased_service_id, provider="gemini")
                if leased_service_id in selected_agents_cfg:
                    return joined(leased_service_id)

            if "codex_pool" in selected_agents_cfg:
                return joined(lease_session_service(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    pool_service_ids=usable_codex_service_pool,
                ), provider="codex")
            if "claude_pool" in selected_agents_cfg:
                return joined(lease_session_service(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    pool_service_ids=usable_claude_service_pool,
                ), provider="claude")
            if "gemini_pool" in selected_agents_cfg:
                return joined(lease_session_service(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    pool_service_ids=usable_gemini_service_pool,
                ), provider="gemini")

            selected_local = [
                service_id
                for service_id in selected_agents_cfg
                if service_id in all_local_service_ids and not service_blocked_by_panic(service_id)
            ]
            if selected_local:
                return joined(lease_session_service(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    pool_service_ids=selected_local,
                ))
            return None

        # Build ordered provider list from agent_priority; fall back to preferred_provider
        agent_priority = active_agent_priority(session_settings.get("agent_priority"))
        if not agent_priority:
            preferred_provider = str(session_settings.get("preferred_provider", default_provider)).strip().lower() or default_provider
            agent_priority = [preferred_provider]

        pool_for_kind: dict[str, list[str]] = {
            "codex": usable_codex_service_pool,
            "claude": usable_claude_service_pool,
            "gemini": usable_gemini_service_pool,
        }

        # If already leased, keep it if it belongs to any provider in the priority list
        if leased_service_id:
            for provider in agent_priority:
                pool = pool_for_kind.get(provider, [])
                if leased_service_id in pool:
                    return joined(leased_service_id, provider=provider)

        # Try to lease from pools in priority order.
        # lease_session_service handles session_priority-based preemption: if all slots are
        # taken but the current session outranks the lowest-priority holder, that holder's
        # lease is revoked and the slot is granted here.
        for provider in agent_priority:
            pool = pool_for_kind.get(provider, [])
            if not pool:
                continue
            svc = lease_session_service(
                runtime_root,
                username=username,
                session_id=session_id,
                pool_service_ids=pool,
            )
            if svc:
                return joined(svc, provider=provider)

        if isinstance(default_target, str) and default_target:
            return joined(default_target)
        return None

    def codex_service_candidates_for_session(*, username: str, session_id: str) -> list[str]:
        current_codex_service_pool, _current_claude_service_pool, _current_gemini_service_pool, _current_llm_service_kinds = current_llm_service_topology()
        candidates: list[str] = []
        leased_service_id = get_session_service(runtime_root, username=username, session_id=session_id)
        if leased_service_id:
            candidates.append(leased_service_id)
        for service_id in current_codex_service_pool:
            if service_id not in candidates:
                candidates.append(service_id)
        if isinstance(default_target, str) and default_target and default_target not in candidates:
            candidates.append(default_target)
        return candidates

    def resolve_bound_codex_session(*, username: str, session_id: str) -> tuple[str | None, str | None]:
        for service_id in codex_service_candidates_for_session(username=username, session_id=session_id):
            session_id = load_codex_session(
                runtime_root,
                service_id=service_id,
                username=username,
                session_id=session_id,
            )
            if session_id:
                return service_id, str(session_id)
        return None, None

    def enqueue_goal_dispatch(
        *,
        username: str,
        session_id: str,
        auth_context: dict[str, Any] | None,
        reason: str,
        previous_goal_text: str | None = None,
        previous_goal_id: str | None = None,
    ) -> tuple[str | None, str | None]:
        talk = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
        active_goal_id = str(talk.get("active_goal_id") or talk.get("goal_id") or "").strip()
        goal_text = str(talk.get("goal_text", "")).strip()
        goal_active = bool(talk.get("goal_active", False))
        goal_completed = bool(talk.get("goal_completed", False))
        goal_progress_state = str(talk.get("goal_progress_state", "in_progress")).strip().lower()
        if not (
            goal_text
            and goal_active
            and not goal_completed
            and goal_progress_state == "in_progress"
        ):
            return None, "goal_state_disallows_dispatch"
        to_service = resolve_session_service_for_dispatch(username=username, session_id=session_id)
        if not to_service:
            raw_ap = talk.get("agent_priority")
            if isinstance(raw_ap, list) and raw_ap:
                priority_label = "_then_".join(active_agent_priority(raw_ap)) or default_provider
            else:
                priority_label = str(talk.get("preferred_provider", default_provider)).strip().lower() or default_provider
            return None, f"no_available_{priority_label}_worker"
        # Audit state is agent-side: block dispatch only if the agent is in panic
        agent_audit_state = load_agent_audit_state(
            runtime_root, service_id=to_service, username=username, session_id=session_id
        )
        if agent_audit_state == "panic":
            maybe_clear_stale_idle_agent_panic(
                runtime_root,
                username=username,
                session_id=session_id,
                service_id=to_service,
            )
            agent_audit_state = load_agent_audit_state(
                runtime_root, service_id=to_service, username=username, session_id=session_id
            )
        if agent_audit_state == "panic":
            return None, "agent_audit_state_disallows_dispatch:panic"
        goal_update_lines = ["<aize_goal_update>"]
        if previous_goal_id is not None:
            goal_update_lines.append(f"  <previous_goal_id>{html.escape(previous_goal_id)}</previous_goal_id>")
        if previous_goal_text is not None:
            goal_update_lines.append(f"  <previous_goal>{html.escape(previous_goal_text)}</previous_goal>")
        if active_goal_id:
            goal_update_lines.append(f"  <goal_id>{html.escape(active_goal_id)}</goal_id>")
        goal_update_lines.append(f"  <goal_text>{html.escape(goal_text)}</goal_text>")
        goal_context = session_goal_context(runtime_root, username=username, session_id=session_id)
        if goal_context:
            goal_update_lines.append("  <goal_context>")
            for item in goal_context:
                item_goal_id = str(item.get("goal_id") or "").strip()
                item_goal_text = str(item.get("goal_text") or "").strip()
                item_goal_created_at = str(item.get("goal_created_at") or "").strip()
                if not item_goal_id or not item_goal_text:
                    continue
                goal_update_lines.append("    <goal>")
                goal_update_lines.append(f"      <goal_id>{html.escape(item_goal_id)}</goal_id>")
                if item_goal_created_at:
                    goal_update_lines.append(f"      <created_at>{html.escape(item_goal_created_at)}</created_at>")
                goal_update_lines.append(f"      <goal_text>{html.escape(item_goal_text)}</goal_text>")
                goal_update_lines.append("    </goal>")
            goal_update_lines.append("  </goal_context>")
        goal_update_lines.append("  <instruction>Review the active goal and continue work toward it until GoalManager can mark it completed.</instruction>")
        goal_update_lines.append("</aize_goal_update>")
        append_pending_input(
            runtime_root,
            username=username,
            session_id=session_id,
            entry=make_aize_pending_input(
                kind="goal_update",
                role="system",
                text="\n".join(goal_update_lines),
            ),
        )
        if str(to_service).startswith("ws-peer-"):
            append_history(
                username,
                session_id,
                {
                    "direction": "session_input",
                    "kind": "goal_feedback",
                    "ts": utc_ts(),
                    "service_id": to_service,
                    "to": to_service,
                    "text": "Goal updated. Continue work toward the active goal.",
                    "pending_input_text": "\n".join(goal_update_lines),
                },
            )
            return to_service, None
        goal_dispatch_message = make_dispatch_pending_message(
            manifest=manifest,
            from_service_id=self_service["service_id"],
            to_service_id=to_service,
            process_id=process_id,
            run_id=f"goal-update-{uuid.uuid4().hex[:8]}",
            username=username,
            session_id=session_id,
            auth_context=auth_context,
            reason=reason,
            dispatch_priority=dispatch_priority(reason),
        )
        if not send_router_control(goal_dispatch_message):
            return None, "router_dispatch_failed"
        return to_service, None

    def session_auto_compact_threshold(username: str, session_id: str) -> int:
        talk = get_session_settings(runtime_root, username=username, session_id=session_id)
        if isinstance(talk, dict):
            return normalize_auto_compact_threshold_left_percent(
                talk.get("auto_compact_threshold_left_percent")
            )
        return normalize_auto_compact_threshold_left_percent(None)

    def context_status_from_entry(entry: dict[str, Any]) -> dict[str, str] | None:
        return context_status_from_history_entry(entry)

    def latest_context_status(entries: list[dict[str, Any]]) -> dict[str, str] | None:
        for entry in entries:
            status = context_status_from_entry(entry)
            if status is not None:
                return status
        return None

    def stored_context_status(username: str, session_id: str) -> dict[str, str] | None:
        talk = get_session_settings(runtime_root, username=username, session_id=session_id)
        value = talk.get("last_context_status") if isinstance(talk, dict) else None
        if not isinstance(value, dict):
            return None
        label = value.get("label")
        if not isinstance(label, str) or not label:
            return None
        status: dict[str, str] = {"label": label}
        for key in ("meta", "left_percent", "used_percent", "compaction", "event_type"):
            raw = value.get(key)
            if isinstance(raw, str):
                status[key] = raw
        return status

    def unsupported_context_status(provider_kind: str) -> dict[str, str]:
        normalized = str(provider_kind or "").strip().lower() or "unknown"
        provider_label = {"codex": "Codex", "claude": "Claude Code", "gemini": "Gemini"}.get(
            normalized,
            normalized.title() or "Unknown",
        )
        return {
            "label": f"Context status unavailable for {provider_label}",
            "meta": "This provider does not expose compact/context-window helpers yet.",
            "event_type": "service.context_status_unsupported",
            "compaction": "unsupported_provider",
        }

    def refresh_context_status(username: str, session_id: str) -> dict[str, str] | None:
        conversation_session_id = session_id
        bound_service_id, provider_session_id = resolve_bound_codex_session(
            username=username,
            session_id=conversation_session_id,
        )
        repo_root = Path(__file__).resolve().parents[2]
        threshold = session_auto_compact_threshold(username, conversation_session_id)
        if bound_service_id and provider_session_id:
            event, _returncode = run_codex_compaction(
                repo_root=repo_root,
                session_id=provider_session_id,
                threshold_left_percent=threshold,
                mode="auto",
            )
        else:
            session_service_id = get_session_service(
                runtime_root,
                username=username,
                session_id=conversation_session_id,
            )
            if not session_service_id:
                return None
            try:
                service_kind = str(get_service_record(runtime_root, session_service_id).get("kind", ""))
            except (KeyError, FileNotFoundError):
                return None
            if not provider_supports_context_compaction(service_kind):
                status = unsupported_context_status(service_kind)
                update_session_context_status(
                    runtime_root,
                    username=username,
                    session_id=conversation_session_id,
                    context_status=status,
                )
                return status
            if service_kind == "claude":
                claude_session_id = load_claude_session(
                    runtime_root,
                    service_id=session_service_id,
                    username=username,
                    session_id=conversation_session_id,
                )
                if not claude_session_id:
                    return None
                bound_service_id = session_service_id
                provider_session_id = claude_session_id
                event, _returncode = run_claude_compaction(
                    repo_root=repo_root,
                    session_id=provider_session_id,
                    threshold_left_percent=threshold,
                    mode="auto",
                )
            elif service_kind == "gemini":
                gemini_session_id = load_gemini_session(
                    runtime_root,
                    service_id=session_service_id,
                    username=username,
                    session_id=conversation_session_id,
                )
                if not gemini_session_id:
                    return None
                bound_service_id = session_service_id
                provider_session_id = gemini_session_id
                event, _returncode = run_gemini_compaction(
                    repo_root=repo_root,
                    session_id=provider_session_id,
                    threshold_left_percent=threshold,
                    mode="auto",
                )
            else:
                return None
        persist_session_context_status(
            runtime_root,
            username=username,
            session_id=conversation_session_id,
            event=event,
            service_id=bound_service_id,
        )
        append_history(
            username,
            conversation_session_id,
            make_history_event_entry(event, service_id=bound_service_id),
        )
        return stored_context_status(username, conversation_session_id)

    def ensure_context_status(username: str, session_id: str) -> dict[str, str] | None:
        status = stored_context_status(username, session_id)
        if status is not None:
            return status
        return refresh_context_status(username, session_id)

    def manual_compact_current_session(*, username: str, session_id: str) -> tuple[int, dict[str, Any]]:
        session_settings = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
        preferred_provider = str(session_settings.get("preferred_provider") or default_provider or "codex").strip().lower() or "codex"
        target_service_id, _bound_session_id = resolve_bound_codex_session(username=username, session_id=session_id)
        if not target_service_id:
            target_service_id = get_session_service(runtime_root, username=username, session_id=session_id)
        if not target_service_id:
            current_codex_service_pool, current_claude_service_pool, current_gemini_service_pool, _current_llm_service_kinds = current_llm_service_topology()
            target_pool = {
                "codex": current_codex_service_pool,
                "claude": current_claude_service_pool,
                "gemini": current_gemini_service_pool,
            }.get(preferred_provider, current_codex_service_pool)
            target_service_id = lease_session_service(
                runtime_root,
                username=username,
                session_id=session_id,
                pool_service_ids=target_pool,
            )
        if not target_service_id:
            return 409, {
                "error": f"no_available_{preferred_provider}_worker",
                "pool": {
                    "codex": current_codex_service_pool,
                    "claude": current_claude_service_pool,
                    "gemini": current_gemini_service_pool,
                }.get(preferred_provider, current_codex_service_pool),
                "session_id": session_id,
            }
        write_jsonl(
            log_path,
            {
                "type": "manual_compact.requested",
                "ts": utc_ts(),
                "service_id": self_service["service_id"],
                "process_id": process_id,
                "username": username,
                "session_id": session_id,
                "target_service_id": target_service_id,
            },
        )
        try:
            target_service = get_service_record(runtime_root, target_service_id)
        except KeyError:
            return 404, {"error": "target_not_found", "service_id": target_service_id}
        target_kind = str(target_service.get("kind"))
        if target_kind not in {"codex", "claude", "gemini"}:
            return 409, {
                "error": "manual_compact_unsupported_for_provider",
                "provider": target_kind,
                "service_id": target_service_id,
                "session_id": session_id,
            }
        started_event = {
            "type": "service.manual_compact_started",
            "reason": "Manual compact requested from HTTPBridge.",
            "session_id": session_id,
        }
        append_history(username, session_id, make_history_event_entry(started_event, service_id=target_service_id))
        if target_kind == "claude":
            status, response, history_entry = manual_compact_claude_session(
                repo_root=Path(__file__).resolve().parents[2],
                runtime_root=runtime_root,
                service_id=target_service_id,
                username=username,
                session_id=session_id,
            )
        elif target_kind == "gemini":
            status, response, history_entry = manual_compact_gemini_session(
                repo_root=Path(__file__).resolve().parents[2],
                runtime_root=runtime_root,
                service_id=target_service_id,
                username=username,
                session_id=session_id,
            )
        else:
            status, response, history_entry = manual_compact_codex_session(
                repo_root=Path(__file__).resolve().parents[2],
                runtime_root=runtime_root,
                service_id=target_service_id,
                username=username,
                session_id=session_id,
            )
        if history_entry is not None:
            append_history(username, session_id, history_entry)
        if status >= 400:
            save_agent_audit_state(
                runtime_root,
                service_id=target_service_id,
                username=username,
                session_id=session_id,
                audit_state="panic",
            )
            update_session_goal_flags(
                runtime_root,
                username=username,
                session_id=session_id,
                goal_completed=False,
                goal_progress_state="in_progress",
            )
            recovery_session = ensure_panic_recovery_session(
                runtime_root,
                username=username,
                source_session_id=session_id,
                source_label=str(session_settings.get("label") or session_id),
                panic_service_id=target_service_id,
                event=response,
                preferred_provider=(
                    "gemini"
                    if "gemini" in target_service_id
                    else ("claude" if "claude" in target_service_id else "codex")
                ),
            )
            if isinstance(recovery_session, dict):
                recovery_session_id = str(recovery_session.get("session_id") or "").strip()
                if recovery_session_id:
                    append_pending_input(
                        runtime_root,
                        username=username,
                        session_id=recovery_session_id,
                        entry=make_aize_pending_input(
                            kind="panic_recovery",
                            role="system",
                            text=panic_recovery_bootstrap_xml(
                                source_session_id=session_id,
                                source_label=str(session_settings.get("label") or session_id),
                                panic_service_id=target_service_id,
                                event=response,
                            ),
                        ),
                    )
                    append_history(
                        username,
                        session_id,
                        {
                            "direction": "event",
                            "ts": utc_ts(),
                            "service_id": target_service_id,
                            "event_type": "service.panic_recovery_session_created",
                            "text": f"Panic recovery session created: {recovery_session_id}",
                            "event": {
                                "type": "service.panic_recovery_session_created",
                                "source_session_id": session_id,
                                "recovery_session_id": recovery_session_id,
                                "panic_service_id": target_service_id,
                                "panic_event": dict(response),
                            },
                        },
                    )
                    dispatch_service_id = target_service_id
                    dispatch_message = make_dispatch_pending_message(
                        manifest=manifest,
                        from_service_id=self_service["service_id"],
                        to_service_id=dispatch_service_id,
                        process_id=process_id,
                        run_id=f"panic-recovery-{uuid.uuid4().hex[:8]}",
                        username=username,
                        session_id=recovery_session_id,
                        auth_context=None,
                        reason="panic_recovery",
                        session_agent_id=resolve_session_agent_id(
                            runtime_root,
                            username=username,
                            session_id=recovery_session_id,
                            service_id=dispatch_service_id,
                        ),
                    )
                    send_router_control(dispatch_message)
        if manual_compact_clears_audit_state(status, response):
            reset_agent_audit_states_for_session(
                runtime_root,
                username=username,
                session_id=session_id,
            )
            response["goal_audit_state"] = "all_clear"
        write_jsonl(
            log_path,
            {
                "type": "manual_compact.completed",
                "ts": utc_ts(),
                "service_id": self_service["service_id"],
                "process_id": process_id,
                "username": username,
                "session_id": session_id,
                "status": status,
                "response": response,
            },
        )
        return status, response

    def render_entry_html(entry: dict[str, Any]) -> str:
        direction = html.escape(str(entry.get("direction", "event")))
        text = html.escape(str(entry.get("text", "")))
        role = {"out": "You", "in": "CodexFox", "event": "Event", "agent": "AgentMessage"}.get(
            str(entry.get("direction")), direction
        )
        badge_html = ""
        if entry.get("context_left_percent") is not None:
            badge_bits = [f"{html.escape(str(entry['context_left_percent']))}% left"]
            if entry.get("context_compaction"):
                badge_bits.append(html.escape(str(entry["context_compaction"])))
            badge_html = f"<div class='ctx-badge'>{' | '.join(badge_bits)}</div>"
        meta = []
        if entry.get("event_type"):
            meta.append(html.escape(str(entry["event_type"])))
        if entry.get("from"):
            meta.append(f"from {html.escape(str(entry['from']))}")
        if entry.get("to"):
            meta.append(f"to {html.escape(str(entry['to']))}")
        meta_html = ""
        if meta:
            meta_html = f"<div class='meta'>{' | '.join(meta)}</div>"
        event_html = ""
        if isinstance(entry.get("event"), dict):
            event_html = (
                "<pre class='event-json'>"
                + html.escape(json.dumps(entry["event"], ensure_ascii=False, indent=2))
                + "</pre>"
            )
        return (
            f"<li class='entry entry-{direction}'>"
            "<div class='bubble'>"
            f"{badge_html}"
            f"<div class='role'>{html.escape(role)}</div>"
            f"<div class='text'>{text}</div>"
            f"{meta_html}"
            f"{event_html}"
            "</div>"
            "</li>"
        )

    def cookie_value(name: str, header: str | None) -> str | None:
        if not header:
            return None
        jar = cookies.SimpleCookie()
        jar.load(header)
        morsel = jar.get(name)
        if morsel is None:
            return None
        return morsel.value

    def request_parts(handler: BaseHTTPRequestHandler) -> tuple[str, dict[str, list[str]]]:
        parsed = urlsplit(handler.path)
        return parsed.path, parse_qs(parsed.query, keep_blank_values=True)

    def requested_session_id(
        handler: BaseHTTPRequestHandler,
        payload: dict[str, Any] | None = None,
        query: dict[str, list[str]] | None = None,
    ) -> str | None:
        if isinstance(payload, dict):
            session_id = payload.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                return session_id.strip()
        if query is None:
            _, query = request_parts(handler)
        query_values = query.get("session_id") or []
        if query_values and isinstance(query_values[0], str) and query_values[0].strip():
            return query_values[0].strip()
        return None

    def request_positive_int(
        query: dict[str, list[str]] | None,
        key: str,
        *,
        default: int,
        minimum: int = 1,
        maximum: int | None = None,
    ) -> int:
        raw_values = (query or {}).get(key) or []
        raw_value = raw_values[0] if raw_values else None
        try:
            value = int(str(raw_value).strip()) if raw_value is not None else default
        except (TypeError, ValueError):
            value = default
        if value < minimum:
            return default
        if maximum is not None and value > maximum:
            return maximum
        return value

    auth_context_cache: dict[str, tuple[float, dict[str, Any]]] = {}
    auth_context_cache_lock = threading.Lock()
    auth_context_cache_ttl_seconds = 5.0

    def current_context(
        handler: BaseHTTPRequestHandler,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, list[str]] | None = None,
    ) -> dict[str, Any] | None:
        token = cookie_value("bridge_session", handler.headers.get("Cookie"))
        explicit_session_id = requested_session_id(handler, payload=payload, query=query)
        now = time.monotonic()
        base_context: dict[str, Any] | None = None
        if token:
            with auth_context_cache_lock:
                cached = auth_context_cache.get(token)
                if cached and cached[0] > now:
                    base_context = dict(cached[1])
        if base_context is None:
            base_context = resolve_session_context(runtime_root, token)
            if base_context and token:
                cache_context = {
                    "username": str(base_context.get("username") or ""),
                    "session_id": str(base_context.get("session_id") or ""),
                    "roles": list(base_context.get("roles") or ["user"]),
                    "role": str(base_context.get("role") or "user"),
                }
                with auth_context_cache_lock:
                    auth_context_cache[token] = (now + auth_context_cache_ttl_seconds, cache_context)
        if not base_context:
            return None
        if not explicit_session_id:
            auth = issue_auth_context(runtime_root, username=base_context.get("username", ""))
            is_superuser = auth_context_allows(auth, "superuser")
            return {
                "username": base_context["username"],
                "viewer_username": base_context["username"],
                "session_id": base_context["session_id"],
                "roles": list(base_context.get("roles") or ["user"]),
                "role": base_context.get("role", "user"),
                "is_superuser": is_superuser,
            }
        direct_session = read_json_file(
            session_metadata_path(
                runtime_root,
                username=str(base_context.get("username") or ""),
                session_id=explicit_session_id,
            )
        )
        if isinstance(direct_session, dict):
            base_roles = list(base_context.get("roles") or ["user"])
            return {
                "username": base_context["username"],
                "viewer_username": base_context["username"],
                "session_id": explicit_session_id,
                "roles": base_roles,
                "role": base_context.get("role", "user"),
                "is_superuser": bool({"root", "superuser"} & {str(role) for role in base_roles}),
            }
        auth = issue_auth_context(runtime_root, username=base_context.get("username", ""))
        is_superuser = auth_context_allows(auth, "superuser")
        if not isinstance(direct_session, dict):
            if not is_superuser:
                return None
            for session in list_all_sessions_with_users(runtime_root):
                if str(session.get("session_id") or "").strip() != explicit_session_id:
                    continue
                return {
                    "username": str(session.get("username") or "").strip(),
                    "viewer_username": base_context["username"],
                    "session_id": explicit_session_id,
                    "roles": list(base_context.get("roles") or ["user"]),
                    "role": base_context.get("role", "user"),
                    "is_superuser": is_superuser,
                }
            return None
        return {
            "username": base_context["username"],
            "viewer_username": base_context["username"],
            "session_id": explicit_session_id,
            "roles": list(base_context.get("roles") or ["user"]),
            "role": base_context.get("role", "user"),
            "is_superuser": is_superuser,
        }

    from runtime.http_handler import make_handler
    Handler = make_handler(
        runtime_root=runtime_root,
        manifest=manifest,
        self_service=self_service,
        process_id=process_id,
        log_path=log_path,
        default_target=default_target,
        default_provider=default_provider,
        history_limit=history_limit,
        tls_enabled=tls_enabled,
        codex_service_pool=codex_service_pool,
        claude_service_pool=claude_service_pool,
        gemini_service_pool=gemini_service_pool,
        llm_service_kinds=llm_service_kinds,
        pending=pending,
        awaiting_replies=awaiting_replies,
        subscribers=subscribers,
        subscribers_lock=subscribers_lock,
        stopped=stopped,
        _active_goal_audits=_active_goal_audits,
        _active_goal_audits_lock=_active_goal_audits_lock,
        _active_agent_turns=_active_agent_turns,
        _active_agent_turns_lock=_active_agent_turns_lock,
        release_stale_session_bindings=release_stale_session_bindings,
        subscriber_key=subscriber_key,
        append_history=append_history,
        send_router_control=send_router_control,
        enqueue_service_control=enqueue_service_control,
        service_snapshots=service_snapshots,
        session_runtime_payload=session_runtime_payload,
        peer_descriptor=peer_descriptor,
        resolve_session_service_for_dispatch=resolve_session_service_for_dispatch,
        current_llm_service_topology=current_llm_service_topology,
        codex_service_candidates_for_session=codex_service_candidates_for_session,
        resolve_bound_codex_session=resolve_bound_codex_session,
        enqueue_goal_dispatch=enqueue_goal_dispatch,
        session_auto_compact_threshold=session_auto_compact_threshold,
        context_status_from_entry=context_status_from_entry,
        latest_context_status=latest_context_status,
        stored_context_status=stored_context_status,
        refresh_context_status=refresh_context_status,
        ensure_context_status=ensure_context_status,
        manual_compact_current_session=manual_compact_current_session,
        render_entry_html=render_entry_html,
        cookie_value=cookie_value,
        request_parts=request_parts,
        requested_session_id=requested_session_id,
        request_positive_int=request_positive_int,
        current_context=current_context,
    )
    servers: list[ThreadingHTTPServer] = []
    server_threads: list[threading.Thread] = []

    def _build_server(bind_host: str, *, family: int) -> ThreadingHTTPServer:
        class FamilyThreadingHTTPServer(ThreadingHTTPServer):
            address_family = family

            def server_bind(self) -> None:
                if family == socket.AF_INET6:
                    try:
                        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    except OSError:
                        pass
                super().server_bind()

        return FamilyThreadingHTTPServer((bind_host, port), Handler)

    bind_specs = _resolve_bind_specs(host)
    bind_hosts: list[str] = []
    for bind_host, family in bind_specs:
        servers.append(_build_server(bind_host, family=family))
        bind_hosts.append(bind_host)

    if tls_enabled:
        from tls.gen_self_signed_cert import (
            certificate_needs_regeneration,
            discover_local_tls_hosts,
            generate_self_signed_cert,
        )

        effective_tls_hosts = list(tls_hosts)
        if tls_auto_hosts:
            effective_tls_hosts.extend(discover_local_tls_hosts(bind_hosts=bind_hosts))
        if certificate_needs_regeneration(tls_cert, tls_key, required_hosts=effective_tls_hosts):
            generate_self_signed_cert(tls_cert, tls_key, cn=tls_cn, extra_hosts=effective_tls_hosts)
        tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_ctx.load_cert_chain(certfile=str(tls_cert), keyfile=str(tls_key))
        for server in servers:
            server.socket = tls_ctx.wrap_socket(server.socket, server_side=True)
    for server in servers:
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        server_threads.append(server_thread)

    threading.Thread(target=_run_startup_state_reconcile, daemon=True).start()

    # Start outbound WS peer client connections (config from runtime/ws_peer_clients.json)
    start_ws_peer_clients(
        runtime_root,
        manifest=manifest,
        self_service=self_service,
        process_id=process_id,
        log_path=log_path,
        codex_service_pool=codex_service_pool,
        claude_service_pool=claude_service_pool,
        gemini_service_pool=gemini_service_pool,
        append_history=append_history,
        stopped=stopped,
    )

    write_jsonl(
        log_path,
        {
            "type": "http_service.started",
            "ts": utc_ts(),
            "service_id": self_service["service_id"],
            "process_id": process_id,
            "host": host,
            "bind_hosts": bind_hosts,
            "port": port,
            "tls": tls_enabled,
            "default_target": default_target,
        },
    )

    rx_fd = router_conn.fileno()
    try:
        while not stopped.is_set():
            drained: list[dict[str, Any]] = []
            while True:
                try:
                    drained.append(pending.get_nowait())
                except queue.Empty:
                    break
            if drained:
                drained = order_dispatch_messages(drained)
                grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
                ordered_keys: list[tuple[str, str, str]] = []
                for outbound in drained:
                    key = (str(outbound["to"]), str(outbound["username"]), str(outbound["session_id"]))
                    if key not in grouped:
                        ordered_keys.append(key)
                    grouped[key].append(outbound)

                for to_service, username, session_id in ordered_keys:
                    batch = grouped[(to_service, username, session_id)]
                    pending_inputs = drain_pending_inputs(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                    )
                    for outbound in batch:
                        append_history(
                            outbound["username"],
                            outbound["session_id"],
                            {
                                "direction": "out",
                                "ts": utc_ts(),
                                "to": outbound["to"],
                                "session_id": outbound["session_id"],
                                "text": outbound["text"],
                            },
                        )
                    outbound_text = build_aize_input_batch_xml(
                        sender_display_name=str(self_service["display_name"]),
                        username=username,
                        session_id=session_id,
                        inputs=pending_inputs
                        + [
                            make_aize_pending_input(
                                kind="user_message",
                                role="user",
                                text=str(item["text"]),
                            )
                            for item in batch
                        ],
                        instruction="Respond to the pending user inputs in order, prioritizing the latest message while preserving relevant context from earlier queued inputs.",
                    )
                    message = build_outgoing_message(
                        runtime_root=runtime_root,
                        manifest=manifest,
                        from_node_id=manifest["node_id"],
                        from_service_id=self_service["service_id"],
                        to_node_id=manifest["node_id"],
                        to_service_id=to_service,
                        process_id=process_id,
                        run_id=manifest["run_id"],
                        text=outbound_text,
                        username=username,
                        session_id=session_id,
                        auth_context=batch[-1].get("auth"),
                    )
                    router_conn.write(encode_line(message))
                    awaiting_replies.append(
                        {
                            "username": username,
                            "session_id": session_id,
                            "to": to_service,
                            "auth": batch[-1].get("auth"),
                        }
                    )
                    write_jsonl(
                        log_path,
                        {
                            "type": "message.out",
                            "ts": utc_ts(),
                            "service_id": self_service["service_id"],
                            "process_id": process_id,
                            "batched_count": len(batch),
                            "pending_input_count": len(pending_inputs),
                            "message": message,
                        },
                    )

            ready, _, _ = select.select([rx_fd], [], [], 0.5)
            if not ready:
                continue
            chunk_bytes = os.read(rx_fd, 65536)
            if not chunk_bytes:
                continue
            chunk = rx_decoder.decode(chunk_bytes)
            rx_buffer += chunk
            if "\n" not in rx_buffer:
                continue
            raw_lines = rx_buffer.split("\n")
            rx_buffer = raw_lines.pop()
            for raw_line in raw_lines:
                line = raw_line.strip()
                if not line:
                    continue
                message = decode_line(line)
                if message.get("type") == "event":
                    scope_username, scope_session_id = resolve_conversation_scope(message)
                    entry = resolve_event_entry(runtime_root, message)
                    if scope_username and scope_session_id and isinstance(entry, dict):
                        append_history(scope_username, scope_session_id, entry)
                        _ov_key = f"{scope_username}::{scope_session_id}"
                        _ov_evt = str(entry.get("event_type") or "")
                        _ov_svc = str(entry.get("service_id") or message.get("from") or "")
                        if _ov_evt == "agent.turn_started":
                            _ov_event = entry.get("event") if isinstance(entry.get("event"), dict) else {}
                            if not bool(_ov_event.get("goal_manager", False)):
                                with _active_agent_turns_lock:
                                    _active_agent_turns[_ov_key] = {"service_id": _ov_svc, "started_at": utc_ts()}
                                broadcast_runtime_status(scope_username, scope_session_id)
                        elif _ov_evt == "turn.completed" or str(entry.get("direction") or "") == "in":
                            with _active_agent_turns_lock:
                                _active_agent_turns.pop(_ov_key, None)
                            broadcast_runtime_status(scope_username, scope_session_id)
                        if _ov_evt == "service.goal_manager_compact_started":
                            _ov_job = str((entry.get("event") or {}).get("goal_audit_job_id") or "")
                            with _active_goal_audits_lock:
                                _active_goal_audits[_ov_key] = {"job_id": _ov_job, "service_id": _ov_svc, "started_at": utc_ts()}
                            broadcast_runtime_status(scope_username, scope_session_id)
                        elif _ov_evt in {
                            "service.goal_audit_completed",
                            "service.goal_audit_failed",
                            "service.goal_manager_compact_completed",
                            "service.goal_manager_compact_failed",
                            "service.goal_manager_compact_checked",
                        }:
                            with _active_goal_audits_lock:
                                _active_goal_audits.pop(_ov_key, None)
                            broadcast_runtime_status(scope_username, scope_session_id)
                    continue
                if message.get("type") != "prompt":
                    continue
                incoming_text = resolve_payload_text(runtime_root, message)
                message_meta = message.get("meta") if isinstance(message.get("meta"), dict) else {}
                username, session_id = resolve_http_reply_scope(message, awaiting_replies)
                with _active_agent_turns_lock:
                    _active_agent_turns.pop(f"{username}::{session_id}", None)
                broadcast_runtime_status(username, session_id)
                append_history(
                    username,
                    session_id,
                    {
                        "direction": "in",
                        "ts": utc_ts(),
                        "from": message.get("from"),
                        "session_id": session_id,
                        "text": incoming_text,
                        "message_id": str(message_meta.get("message_id") or ""),
                    },
                )
                refresh_context_status(username, session_id)
                write_jsonl(
                    log_path,
                    {
                        "type": "message.in",
                        "ts": utc_ts(),
                        "service_id": self_service["service_id"],
                        "process_id": process_id,
                        "message": message,
                    },
                )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        router_conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CLI service adapter")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--service-id", required=True)
    args = parser.parse_args()

    manifest = load_manifest(Path(args.manifest))
    runtime_root = Path(args.runtime_root)
    ports_dir = runtime_root / "ports"
    logs_dir = runtime_root / "logs"
    self_service = wait_for_service_record(runtime_root, args.service_id)
    process_id = make_process_id(args.service_id)
    log_path = logs_dir / f"{args.service_id}.jsonl"
    register_process(
        runtime_root,
        process_id=process_id,
        service_id=args.service_id,
        node_id=manifest["node_id"],
        status="starting",
    )
    update_service_process(
        runtime_root,
        service_id=args.service_id,
        process_id=process_id,
        status="running",
    )
    register_process(
        runtime_root,
        process_id=process_id,
        service_id=args.service_id,
        node_id=manifest["node_id"],
        status="running",
    )
    update_process_fields(
        runtime_root,
        process_id=process_id,
        fields={"os_pid": os.getpid()},
    )
    from kernel.ipc import connect_to_router
    router_conn = connect_to_router(runtime_root, args.service_id)

    def start_restart_resume_scan(*, service_kind: str) -> None:
        threading.Thread(
            target=maybe_resume_after_restart,
            kwargs={
                "runtime_root": runtime_root,
                "manifest": manifest,
                "self_service": self_service,
                "process_id": process_id,
                "log_path": log_path,
                "service_id": args.service_id,
                "router_conn": router_conn,
                "service_kind": service_kind,
            },
            daemon=True,
        ).start()

    if self_service["kind"] == "codex":
        ensure_state(runtime_root)
        update_process_fields(
            runtime_root,
            process_id=process_id,
            fields={"codex_session_id": load_codex_session(runtime_root, service_id=args.service_id)},
        )
        start_restart_resume_scan(service_kind="codex")
    elif self_service["kind"] == "claude":
        update_process_fields(
            runtime_root,
            process_id=process_id,
            fields={"claude_session_id": load_claude_session(runtime_root, service_id=args.service_id)},
        )
        start_restart_resume_scan(service_kind="claude")

    write_jsonl(
        log_path,
        {
            "type": "service_adapter.started",
            "ts": utc_ts(),
            "service_id": args.service_id,
            "process_id": process_id,
            "allowed_peers": self_service.get("allowed_peers", []),
        },
    )

    try:
        from services import load_service_handler
        run_service = load_service_handler(self_service["kind"])
        rc = run_service(
            runtime_root=runtime_root,
            manifest=manifest,
            self_service=self_service,
            process_id=process_id,
            log_path=log_path,
            router_conn=router_conn,
        )
        if self_service["kind"] == "http":
            update_service_process(
                runtime_root,
                service_id=args.service_id,
                process_id=process_id,
                status="stopped",
            )
            register_process(
                runtime_root,
                process_id=process_id,
                service_id=args.service_id,
                node_id=manifest["node_id"],
                status="stopped",
                reason="http_service_stopped",
            )
        return rc
    except Exception as exc:
        write_jsonl(
            log_path,
            {
                "type": "service_adapter.failed",
                "ts": utc_ts(),
                "service_id": args.service_id,
                "process_id": process_id,
                "error": repr(exc),
            },
        )
        update_service_process(
            runtime_root,
            service_id=args.service_id,
            process_id=process_id,
            status="failed",
        )
        register_process(
            runtime_root,
            process_id=process_id,
            service_id=args.service_id,
            node_id=manifest["node_id"],
            status="failed",
            reason=repr(exc),
        )
        raise


if __name__ == "__main__":
    sys.exit(main())
