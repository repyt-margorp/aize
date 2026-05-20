from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import urllib.request
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable
from urllib.parse import urlsplit

from runtime.persistent_state_pkg import (
    get_session_settings,
    list_session_agent_contacts,
    load_goal_manager_pending_inputs,
    load_pending_inputs,
    session_dir,
    session_goal_context,
    session_timeline_path,
)
from wire.protocol import message_meta_get

GOAL_AUDIT_BUNDLE_DIRNAME = "goal-audit"
DEFAULT_CODEX_MODEL = str(os.environ.get("AIZE_CODEX_MODEL", "gpt-5.4")).strip() or "gpt-5.4"


def history_excerpt(entries: list[dict[str, Any]], *, limit: int = 24) -> str:
    lines: list[str] = []
    for entry in entries[-limit:]:
        direction = str(entry.get("direction", "event"))
        if direction == "out":
            role = "user"
        elif direction == "in":
            role = "assistant"
        else:
            role = "event"
        text = str(entry.get("text", "")).strip()
        if not text:
            continue
        lines.append(f"[{role}] {text}")
    return "\n".join(lines)


def _record_scope_matches(record: dict[str, Any], *, username: str, session_id: str) -> bool:
    normalized_username = username.strip().lower()
    candidate_scopes: list[dict[str, Any]] = []
    for key in ("scope", "conversation"):
        value = record.get(key)
        if isinstance(value, dict):
            candidate_scopes.append(value)
    message = record.get("message")
    if isinstance(message, dict):
        conversation = message_meta_get(message, "conversation")
        if isinstance(conversation, dict):
            candidate_scopes.append(conversation)
    event = record.get("event")
    if isinstance(event, dict):
        scope = event.get("scope")
        if isinstance(scope, dict):
            candidate_scopes.append(scope)
    for scope in candidate_scopes:
        scope_username = scope.get("username")
        scope_session_id = scope.get("session_id") or scope.get("session_id")
        if (
            isinstance(scope_username, str)
            and isinstance(scope_session_id, str)
            and scope_username.strip().lower() == normalized_username
            and scope_session_id == session_id
        ):
            return True
    return False


def _record_run_ids(record: dict[str, Any]) -> set[str]:
    run_ids: set[str] = set()
    for candidate in (record.get("run_id"),):
        if isinstance(candidate, str) and candidate:
            run_ids.add(candidate)
    message = record.get("message")
    if isinstance(message, dict):
        candidate = message_meta_get(message, "run_id")
        if isinstance(candidate, str) and candidate:
            run_ids.add(candidate)
    event = record.get("event")
    if isinstance(event, dict):
        candidate = event.get("run_id")
        if isinstance(candidate, str) and candidate:
            run_ids.add(candidate)
    return run_ids


def _iter_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append({"source_log": path.name, "line": line_no, "record": record})
    except OSError:
        return records
    return records


def build_goal_audit_log_bundle(*, runtime_root: Path, username: str, session_id: str) -> tuple[Path, int]:
    repo_root = Path(__file__).resolve().parents[2]
    bundle_dir = repo_root / ".temp" / GOAL_AUDIT_BUNDLE_DIRNAME
    bundle_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{username.strip().lower()}-{session_id}")[:80] or "talk"
    logs_dir = runtime_root / "logs"
    staged_records: list[dict[str, Any]] = []
    relevant_run_ids: set[str] = set()
    for path in sorted(logs_dir.glob("*.jsonl")):
        for item in _iter_jsonl_records(path):
            record = item["record"]
            if _record_scope_matches(record, username=username, session_id=session_id):
                staged_records.append(item)
                relevant_run_ids.update(_record_run_ids(record))
    seen_keys = {
        (str(item["source_log"]), int(item["line"]))
        for item in staged_records
    }
    if relevant_run_ids:
        for path in sorted(logs_dir.glob("*.jsonl")):
            for item in _iter_jsonl_records(path):
                key = (str(item["source_log"]), int(item["line"]))
                if key in seen_keys:
                    continue
                if _record_run_ids(item["record"]) & relevant_run_ids:
                    staged_records.append(item)
                    seen_keys.add(key)
    staged_records.sort(
        key=lambda item: (
            str(item["record"].get("ts") or item["record"].get("event", {}).get("ts") or ""),
            str(item["source_log"]),
            int(item["line"]),
        )
    )
    bundle_path = bundle_dir / f"{slug}.jsonl"
    with NamedTemporaryFile("w", encoding="utf-8", dir=bundle_dir, prefix=f"{slug}.", suffix=".tmp", delete=False) as handle:
        temp_path = Path(handle.name)
        for item in staged_records:
            handle.write(
                json.dumps(
                    {
                        "source_log": item["source_log"],
                        "line": item["line"],
                        "record": item["record"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    temp_path.replace(bundle_path)
    return bundle_path, len(staged_records)


def pending_turn_completed_events_since_last_review(
    history_entries: list[dict[str, Any]],
    *,
    last_reviewed_turn_completed_at: str,
) -> list[dict[str, str]]:
    pending: list[dict[str, str]] = []
    cursor = str(last_reviewed_turn_completed_at or "").strip()
    for entry in history_entries:
        if str(entry.get("event_type") or "") != "turn.completed":
            continue
        completed_at = str(
            entry.get("completed_at")
            or entry.get("event", {}).get("completed_at")
            or entry.get("ts")
            or ""
        ).strip()
        if cursor and completed_at and completed_at <= cursor:
            continue
        pending.append(
            {
                "service_id": str(entry.get("service_id") or entry.get("from") or "").strip(),
                "provider": str(entry.get("event", {}).get("provider") or entry.get("provider") or "").strip(),
                "status": str(entry.get("event", {}).get("status") or entry.get("status") or "").strip(),
                "completed_at": completed_at,
                "reply_index": str(
                    entry.get("event", {}).get("reply_index") or entry.get("reply_index") or ""
                ).strip(),
                "text": str(entry.get("text") or "").strip(),
            }
        )
    return pending


def parse_turn_completed_input_xml(text: str) -> dict[str, str]:
    def extract(tag: str) -> str:
        match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        return html.unescape(match.group(1)).strip() if match else ""

    return {
        "service_id": extract("service_id"),
        "reply_index": extract("reply_index"),
        "process_id": extract("process_id"),
        "run_id": extract("run_id"),
        "completed_at": extract("completed_at"),
        "latest_reply": extract("latest_reply"),
    }


def pending_turn_completed_inputs_since_last_review(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    last_reviewed_turn_completed_at: str,
) -> list[dict[str, str]]:
    pending: list[dict[str, str]] = []
    cursor = str(last_reviewed_turn_completed_at or "").strip()

    def append_if_pending(parsed: dict[str, str]) -> None:
        completed_at = str(parsed.get("completed_at") or "").strip()
        if cursor and completed_at and completed_at <= cursor:
            return
        if parsed.get("service_id"):
            pending.append(parsed)

    pending_inputs = load_pending_inputs(runtime_root, username=username, session_id=session_id)
    for item in pending_inputs:
        if str(item.get("kind") or "") != "turn_completed":
            continue
        append_if_pending(parse_turn_completed_input_xml(str(item.get("text") or "")))

    goal_manager_inputs = load_goal_manager_pending_inputs(
        runtime_root,
        username=username,
        session_id=session_id,
    )
    for item in goal_manager_inputs:
        if str(item.get("kind") or "").strip().lower() != "turn_completed":
            continue
        append_if_pending(
            {
                "service_id": str(item.get("service_id") or "").strip(),
                "reply_index": str(item.get("reply_index") or "").strip(),
                "process_id": str(item.get("process_id") or "").strip(),
                "run_id": str(item.get("run_id") or "").strip(),
                "completed_at": str(item.get("completed_at") or item.get("ts") or "").strip(),
                "latest_reply": str(item.get("latest_reply") or "").strip(),
            }
        )
    return pending


def extract_artifact_references(text: str) -> list[str]:
    refs: list[str] = []
    for match in re.findall(r"https?://[^\s<>()`'\"]+|file://[^\s<>()`'\"]+", text):
        if match not in refs:
            refs.append(match)
    for match in re.findall(r"`(/[^`]+|\.{1,2}/[^`]+)`", text):
        if match not in refs:
            refs.append(match)
    return refs


def fetch_and_verify_artifact_reference(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    service_id: str,
    reference: str,
) -> dict[str, Any]:
    artifacts_dir = runtime_root.parent / ".temp" / "goal-artifacts" / f"{username.lower()}-{session_id}"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    normalized_reference = str(reference or "").strip()
    split = urlsplit(normalized_reference)
    if split.scheme in {"http", "https", "file"}:
        source_name = Path(split.path or "artifact.bin").name or "artifact.bin"
    else:
        source_name = Path(normalized_reference).name or "artifact.bin"
    digest = hashlib.sha256(normalized_reference.encode("utf-8")).hexdigest()[:12]
    destination = artifacts_dir / f"{service_id}-{digest}-{source_name}"
    if split.scheme in {"http", "https", "file"}:
        with urllib.request.urlopen(normalized_reference, timeout=10) as response:
            payload = response.read()
        destination.write_bytes(payload)
    else:
        source_path = Path(normalized_reference)
        if not source_path.is_absolute():
            source_path = (runtime_root.parent / source_path).resolve()
        shutil.copyfile(source_path, destination)
    payload = destination.read_bytes()
    validation = "downloaded"
    details: dict[str, Any] = {}
    if destination.suffix.lower() == ".json":
        details["json"] = json.loads(payload.decode("utf-8"))
        validation = "json_valid"
    else:
        try:
            preview = payload.decode("utf-8")
        except UnicodeDecodeError:
            preview = ""
        if preview:
            details["text_preview"] = preview[:200]
            validation = "text_read"
    return {
        "service_id": service_id,
        "reference": normalized_reference,
        "local_path": str(destination),
        "size_bytes": len(payload),
        "validation": validation,
        "details": details,
    }


def collect_and_verify_turn_completed_artifacts(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    last_reviewed_turn_completed_at: str,
) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for item in pending_turn_completed_inputs_since_last_review(
        runtime_root,
        username=username,
        session_id=session_id,
        last_reviewed_turn_completed_at=last_reviewed_turn_completed_at,
    ):
        latest_reply = str(item.get("latest_reply") or "")
        for reference in extract_artifact_references(latest_reply):
            try:
                artifact = fetch_and_verify_artifact_reference(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    service_id=str(item.get("service_id") or ""),
                    reference=reference,
                )
                artifact["completed_at"] = str(item.get("completed_at") or "")
                verified.append(artifact)
            except Exception as exc:
                verified.append(
                    {
                        "service_id": str(item.get("service_id") or ""),
                        "reference": reference,
                        "validation": "error",
                        "error": repr(exc),
                        "completed_at": str(item.get("completed_at") or ""),
                    }
                )
    return verified


def goal_followup_dispatch_targets(
    contacted_agents: list[dict[str, Any]],
    directives: list[dict[str, Any]],
) -> list[str]:
    targets: list[str] = []
    for service_id in [
        str(item.get("service_id") or "").strip()
        for item in contacted_agents
        if isinstance(item, dict)
    ] + [
        str(item.get("service_id") or "").strip()
        for item in directives
        if isinstance(item, dict)
    ]:
        if service_id and service_id not in targets:
            targets.append(service_id)
    return targets


def build_goal_audit_prompt(
    *,
    goal_text: str,
    log_bundle_path: Path,
    log_record_count: int,
    contacted_agents: list[dict[str, Any]],
    pending_turn_completed_events: list[dict[str, str]],
    last_reviewed_turn_completed_at: str,
    agent_welcome_enabled: bool,
    verified_artifacts: list[dict[str, Any]],
    session_dir_path: Path,
    timeline_path: Path,
    goal_context: list[dict[str, str]],
) -> str:
    contacted_agent_lines = [
        json.dumps(
            {
                "service_id": str(item.get("service_id") or ""),
                "provider": str(item.get("provider") or ""),
                "welcomed_at": str(item.get("welcomed_at") or ""),
                "last_turn_completed_at": str(item.get("last_turn_completed_at") or ""),
            },
            ensure_ascii=False,
        )
        for item in contacted_agents
        if isinstance(item, dict) and str(item.get("service_id") or "").strip()
    ]
    pending_turn_completed_lines = [
        json.dumps(item, ensure_ascii=False)
        for item in pending_turn_completed_events
    ]
    verified_artifact_lines = [
        json.dumps(item, ensure_ascii=False)
        for item in verified_artifacts
    ]
    return "\n".join(
        [
            "You are GoalManager for an AIze talk.",
            "Assess whether the goal has been achieved based on the conversation so far.",
            "You must verify against the full scoped JSONL log bundle before deciding, not just the conversation excerpt.",
            "Read the bundle as the source of truth for what actually happened in this session, including prior GoalManager feedback, retries, and compact-related events.",
            "Do NOT load the entire JSONL bundle in one read call when it may be large.",
            "Use targeted inspection first: grep for relevant event types, then read only narrow sections with head/tail/sed-style ranges or offset/limit reads.",
            "If a file-read tool reports size or token limits, switch to targeted searches and partial reads instead of retrying the same full-file read.",
            "This session may involve multiple agents mixed together under one goal; reason across the whole session, not only the current worker.",
            f"Session-level additional-agent welcome signal: {'enabled' if agent_welcome_enabled else 'disabled'}.",
            "Treat the welcomed agent roster as the set of agents that may receive follow-up work for this goal.",
            "Aggregate every TurnCompleted that happened after the last GoalManager review before deciding whether the goal is complete.",
            "If several agents reached TurnCompleted since the last review, combine all of them into one decision.",
            "For external agents, use each TurnCompleted payload and log evidence to decide whether files or artifacts must be downloaded locally, inspected, or validated before you decide.",
            "IMPORTANT: The `goal_completed` field in `service.post_turn_goal_state` log records reflects the PRIOR audit's decision, not the ground truth.",
            "Do NOT use `goal_completed=false` in the log as evidence that the goal is still incomplete — that flag is set by previous audit runs and may lag behind the actual state.",
            "Similarly, prior audits returning goal_satisfied=false does NOT mean the goal is still incomplete; those were assessments at that point in time.",
            "You MUST make your own independent determination: inspect the codebase, run relevant tests, or check runtime state directly.",
            "If prior audits returned in_progress but the agent's replies in this session show the work is now done, use progress_state='complete'.",
            "Before you return progress_state='complete', confirm both the positive requested result and the negative side: check that nearby promised behavior or visible status signals were not regressed by the change.",
            "Do not accept a child session's 'done' claim on positive evidence alone when the touched area also carries neighboring UI/runtime invariants. Look for violated existing behavior and treat any unresolved regression as in_progress.",
            "When relevant, name the exact invariant families you checked: goal state visibility, runtime state visibility, routing/session lineage, permissions, or other behavior directly adjacent to the changed code.",
            "Return JSONL — output one JSON object per line, no markdown fences, no extra text.",
            "Line 1 is always the goal_state record:",
            '  {"kind": "goal_state", "progress_state": "complete" | "in_progress", "goal_satisfied": true | false, "summary": "..."}',
            "Optional later lines are agent_directive records, one per agent that should receive a follow-up now (only when in_progress):",
            "Emit agent_directive records only for agents that should actively continue now.",
            '  {"kind": "agent_directive", "service_id": "...", "audit_state": "all_clear" | "needs_compact" | "panic", "continue_xml": "...", "request_compact": false, "request_compact_reason": "", "summary": ""}',
            "Optional later line when GoalManager needs direct user feedback before the goal can continue:",
            '  {"kind": "user_response_request", "question": "...", "reason": "...", "timeout_seconds": 300}',
            "Only GoalManager may request direct user feedback. Emit at most one user_response_request record. When emitting one, omit agent_directive records unless an agent can do independent work that does not depend on the user answer.",
            "Optional additional lines are child_goal_request records:",
            '  {"kind": "child_goal_request", "goal_text": "...", "label": "", "provider": "codex" | "claude" | "gemini"}',
            "These are GoalManager-owned system signals, not agent actions: GoalManager will later materialize them into child sessions.",
            "Emit child_goal_request records whenever splitting the remaining work into child sessions is useful, including a single child goal when that best matches the work decomposition.",
            "Each child goal remains subordinate to the current goal: the spawned child session inherits parent-goal lineage/context and continues the broader parent objective while focusing on its own subgoal.",
            "Preserve the Root-to-child goal hierarchy explicitly. Do not treat delegated child goals as peer constitutions or independent top-level objectives when the session lineage says they are subgoals.",
            "CRITICAL: When progress_state is 'complete', output ONLY the goal_state line. Do NOT output any agent_directive or child_goal_request records.",
            'Use progress_state="complete" only when the goal itself is done. Otherwise use "in_progress".',
            "If child sessions are the right next step and no currently welcomed agent needs more work in this session right now, output child_goal_request records and omit agent_directive records.",
            "Do not send agent feedback just because an agent exists in the welcomed roster. Omit agent_directive records when there is no concrete follow-up for that agent yet.",
            'In each agent_directive: use audit_state="all_clear" when the agent is making honest progress, "needs_compact" when the session appears sabotaged/stuck and compact should be attempted before normal continuation, and "panic" when sabotage/stuck state remains unresolved after compact failure or the situation is otherwise unsafe.',
            'Treat plan-only, intent-only, acknowledgment-only, or status-only replies as stuck/sabotaged behavior when they consume a TurnCompleted without concrete work advancement, especially after restart_resume or GoalManager feedback. In that case, set audit_state="needs_compact" and request_compact=true rather than all_clear.',
            'Examples of stuck replies that should push toward needs_compact: "I will continue", "next I will...", "I checked and will now...", "resumed and will proceed", or restatements of remaining tasks without edits/tests/results.',
            'Do not use audit_state="all_clear" merely because the agent described a plausible plan. Require evidence of actual advancement from the log bundle, files, tests, commands, or other concrete runtime effects.',
            'In each agent_directive: when audit_state is "all_clear", continue_xml must be a valid XML fragment rooted at <aize_goal_feedback>.',
            'In each agent_directive: when audit_state is "needs_compact" or "panic", continue_xml must be an empty string and request_compact must be true.',
            'In each agent_directive: set request_compact consistently with audit_state — true when audit_state is "needs_compact", false otherwise.',
            'If more agent work is needed, prefer giving agent_directive records for every agent that should continue.',
            "Keep summary concise and factual.",
            "",
            "Goal:",
            goal_text,
            "",
            "Welcomed agents in FIFO order:",
            *(
                contacted_agent_lines
                if contacted_agent_lines
                else ["(none recorded yet)"]
            ),
            "",
            f"Last GoalManager-reviewed TurnCompleted timestamp: {last_reviewed_turn_completed_at or '(none)'}",
            "TurnCompleted events that have not been reviewed yet:",
            *(
                pending_turn_completed_lines
                if pending_turn_completed_lines
                else ["(none explicitly pending; still inspect the log bundle before deciding)"]
            ),
            "",
            "Verified artifact results gathered from pending TurnCompleted evidence:",
            *(
                verified_artifact_lines
                if verified_artifact_lines
                else ["(no artifact references were verified before this audit)"]
            ),
            "",
            "Full scoped JSONL log bundle:",
            str(log_bundle_path),
            f"JSONL record count: {log_record_count}",
            "",
            "Session files:",
            str(session_dir_path),
            f"Timeline JSONL: {timeline_path}",
            "",
            "Goal context to preserve across this session lineage:",
            *(
                [json.dumps(item, ensure_ascii=False) for item in goal_context]
                if goal_context
                else ["(no extra goal context)"]
            ),
        ]
    )


def extract_jsonl_records(text: str) -> list[dict[str, Any]]:
    """Extract a list of JSON objects from a JSONL-formatted string (one per non-empty line)."""
    records: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
        except json.JSONDecodeError:
            pass
    return records


def default_goal_continue_xml(*, summary: str) -> str:
    return (
        "<aize_goal_feedback>"
        "<status>incomplete</status>"
        f"<summary>{html.escape(summary)}</summary>"
        "<instruction>Continue the work toward the active goal. Fix the missing items described above before reporting completion.</instruction>"
        "</aize_goal_feedback>"
    )


def _normalize_child_goal_requests(
    raw_requests: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    child_goal_requests: list[dict[str, str]] = []
    if isinstance(raw_requests, list):
        for item in raw_requests:
            if not isinstance(item, dict):
                continue
            child_goal_text = str(item.get("goal_text") or "").strip()
            if not child_goal_text:
                continue
            child_goal_request = {
                "goal_text": child_goal_text,
            }
            child_label = str(item.get("label") or "").strip()
            if child_label:
                child_goal_request["label"] = child_label
            child_provider = str(item.get("provider") or "").strip().lower()
            if child_provider in {"codex", "claude", "gemini"}:
                child_goal_request["provider"] = child_provider
            child_service_id = str(item.get("service_id") or "").strip()
            if child_service_id:
                child_goal_request["service_id"] = child_service_id
            child_goal_requests.append(child_goal_request)
    return child_goal_requests


def goal_audit_should_enqueue_agent_followup(
    *,
    progress_state: str | None,
    audit_state: str | None,
    agent_directives: list[dict[str, Any]] | None = None,
) -> bool:
    normalized_progress_state = str(progress_state or "").strip().lower()
    normalized_audit_state = str(audit_state or "").strip().lower()
    has_agent_directives = bool(agent_directives)
    return (
        normalized_progress_state == "in_progress"
        and normalized_audit_state == "all_clear"
        and has_agent_directives
    )


def run_goal_audit(
    *,
    runtime_root: Path,
    username: str,
    session_id: str,
    goal_text: str,
    history_entries: list[dict[str, Any]],
    provider_kind: str = "codex",
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    from runtime import cli_service_adapter as _runtime_cli_service_adapter

    def _run_codex_goal_audit(
        request_prompt: str,
        *,
        request_session_id: str | None,
    ) -> tuple[str, list[dict[str, Any]], str | None]:
        return _runtime_cli_service_adapter.run_codex(
            request_prompt,
            session_id=request_session_id,
            response_schema_id=None,
            on_event=on_event,
        )

    session_settings = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
    contacted_agents = list_session_agent_contacts(runtime_root, username=username, session_id=session_id)
    last_reviewed_turn_completed_at = str(
        session_settings.get("goal_manager_last_reviewed_turn_completed_at", "")
    ).strip()
    pending_turn_completed_events = pending_turn_completed_events_since_last_review(
        history_entries,
        last_reviewed_turn_completed_at=last_reviewed_turn_completed_at,
    )
    verified_artifacts = collect_and_verify_turn_completed_artifacts(
        runtime_root,
        username=username,
        session_id=session_id,
        last_reviewed_turn_completed_at=last_reviewed_turn_completed_at,
    )
    log_bundle_path, log_record_count = build_goal_audit_log_bundle(
        runtime_root=runtime_root,
        username=username,
        session_id=session_id,
    )
    prompt = build_goal_audit_prompt(
        goal_text=goal_text,
        log_bundle_path=log_bundle_path,
        log_record_count=log_record_count,
        contacted_agents=contacted_agents,
        pending_turn_completed_events=pending_turn_completed_events,
        last_reviewed_turn_completed_at=last_reviewed_turn_completed_at,
        agent_welcome_enabled=bool(session_settings.get("agent_welcome_enabled", False)),
        verified_artifacts=verified_artifacts,
        session_dir_path=session_dir(runtime_root, username=username, session_id=session_id),
        timeline_path=session_timeline_path(runtime_root, username=username, session_id=session_id),
        goal_context=session_goal_context(runtime_root, username=username, session_id=session_id),
    )
    requested_provider_kind = str(provider_kind or "").strip().lower()
    preferred_provider = str(session_settings.get("preferred_provider") or "").strip().lower()
    if requested_provider_kind in {"codex", "claude", "gemini"}:
        normalized_provider_kind = requested_provider_kind
    elif preferred_provider in {"codex", "claude", "gemini"}:
        normalized_provider_kind = preferred_provider
    else:
        normalized_provider_kind = requested_provider_kind
    provider_candidates: list[str] = []
    for candidate in (
        normalized_provider_kind,
        preferred_provider,
        requested_provider_kind,
        "codex",
        "claude",
        "gemini",
    ):
        normalized_candidate = str(candidate or "").strip().lower()
        if normalized_candidate in {"codex", "claude", "gemini"} and normalized_candidate not in provider_candidates:
            provider_candidates.append(normalized_candidate)
    if not provider_candidates:
        provider_candidates = ["codex", "claude", "gemini"]
    # JSONL output: no JSON schema enforcement — the prompt instructs the format
    last_provider_error: FileNotFoundError | None = None
    selected_provider_kind = provider_candidates[0]
    final_text = ""
    audit_session_id: str | None = None
    for candidate_provider in provider_candidates:
        try:
            selected_provider_kind = candidate_provider
            if candidate_provider == "claude":
                final_text, _events, audit_session_id = _runtime_cli_service_adapter.run_claude(
                    prompt,
                    session_id=None,
                    response_schema_id=None,
                    on_event=on_event,
                )
            elif candidate_provider == "gemini":
                final_text, _events, audit_session_id = _runtime_cli_service_adapter.run_gemini(
                    prompt,
                    session_id=None,
                    response_schema_id=None,
                    on_event=on_event,
                )
            else:
                final_text, _events, audit_session_id = _run_codex_goal_audit(
                    prompt,
                    request_session_id=None,
                )
            break
        except FileNotFoundError as exc:
            last_provider_error = exc
            if on_event:
                on_event(
                    {
                        "type": "service.goal_audit_provider_unavailable",
                        "provider": candidate_provider,
                        "error": repr(exc),
                    }
                )
    else:
        if last_provider_error is not None:
            raise last_provider_error
        raise RuntimeError("goal_audit_no_provider_available")
    max_parse_retries = 2
    retry_final_text = final_text
    retry_session_id = audit_session_id
    parsed_records: list[dict[str, Any]] | None = None
    parse_error_detail = ""
    for parse_attempt in range(max_parse_retries + 1):
        # Try JSONL first: look for a goal_state record among the lines
        jsonl_records = extract_jsonl_records(retry_final_text)
        goal_state_records = [r for r in jsonl_records if str(r.get("kind", "")).strip() == "goal_state"]
        if goal_state_records:
            parsed_records = jsonl_records
            audit_session_id = retry_session_id
            break
        parse_error_detail = "Expected JSONL with a goal_state record"
        if parse_attempt >= max_parse_retries:
            break
        if on_event:
            on_event({
                "type": "service.goal_manager_compact_parse_retry",
                "attempt": parse_attempt + 1,
                "error": parse_error_detail,
            })
        retry_prompt = (
            "Your previous response could not be parsed.\n"
            f"Parse error: {parse_error_detail}\n\n"
            "Please respond again with JSONL — one JSON object per line, no markdown fences.\n"
            "Line 1 (required): goal_state record:\n"
            '  {"kind": "goal_state", "progress_state": "complete" or "in_progress", "goal_satisfied": true or false, "summary": "..."}\n'
            "Optional later lines (only when in_progress): agent_directive records for agents that should continue now:\n"
            '  {"kind": "agent_directive", "service_id": "...", "audit_state": "all_clear" | "needs_compact" | "panic", "continue_xml": "...", "request_compact": false, "request_compact_reason": "", "summary": ""}\n'
            "Optional later line when GoalManager needs direct user feedback before continuing:\n"
            '  {"kind": "user_response_request", "question": "...", "reason": "...", "timeout_seconds": 300}\n'
            "These lines are optional. Omit them when no agent should receive immediate follow-up.\n"
            "When progress_state is 'complete', output ONLY the goal_state line."
        )
        if selected_provider_kind == "claude":
            retry_final_text, _, retry_session_id = _runtime_cli_service_adapter.run_claude(
                retry_prompt,
                session_id=retry_session_id,
                response_schema_id=None,
                on_event=on_event,
            )
        elif selected_provider_kind == "gemini":
            retry_final_text, _, retry_session_id = _runtime_cli_service_adapter.run_gemini(
                retry_prompt,
                session_id=retry_session_id,
                response_schema_id=None,
                on_event=on_event,
            )
        else:
            retry_final_text, _, retry_session_id = _run_codex_goal_audit(
                retry_prompt,
                request_session_id=retry_session_id,
            )
    if parsed_records is None:
        raise RuntimeError(f"goal_audit_invalid_payload_after_retries: {parse_error_detail}")

    goal_state_rec = next((r for r in parsed_records if str(r.get("kind", "")).strip() == "goal_state"), {})
    raw_agent_directive_recs = [r for r in parsed_records if str(r.get("kind", "")).strip() == "agent_directive"]
    raw_child_goal_recs = [r for r in parsed_records if str(r.get("kind", "")).strip() == "child_goal_request"]
    raw_user_response_recs = [r for r in parsed_records if str(r.get("kind", "")).strip() == "user_response_request"]
    progress_state = str(goal_state_rec.get("progress_state", "")).strip().lower()
    if progress_state not in {"complete", "in_progress"}:
        _gs = goal_state_rec.get("goal_satisfied")
        progress_state = "complete" if bool(_gs) else "in_progress"
    goal_satisfied = progress_state == "complete"
    summary = str(goal_state_rec.get("summary", "")).strip()
    first_directive = raw_agent_directive_recs[0] if raw_agent_directive_recs else {}
    audit_state = str(first_directive.get("audit_state", "")).strip().lower()
    if audit_state not in {"all_clear", "needs_compact", "panic"}:
        audit_state = "needs_compact" if bool(first_directive.get("request_compact", False)) else "all_clear"
    if not first_directive:
        audit_state = "all_clear"
    continue_xml = str(first_directive.get("continue_xml", "")).strip() if audit_state == "all_clear" and not goal_satisfied else ""
    request_compact = audit_state == "needs_compact" or bool(first_directive.get("request_compact", False))
    request_compact_reason = str(first_directive.get("request_compact_reason", "")).strip() if request_compact else ""
    agent_directives: list[dict[str, Any]] = []
    if not goal_satisfied:
        for item in raw_agent_directive_recs:
            directive_service_id = str(item.get("service_id") or "").strip()
            if not directive_service_id:
                continue
            directive_audit_state = str(item.get("audit_state", "")).strip().lower()
            if directive_audit_state not in {"all_clear", "needs_compact", "panic"}:
                directive_audit_state = (
                    "needs_compact" if bool(item.get("request_compact", False)) else "all_clear"
                )
            directive_request_compact = directive_audit_state == "needs_compact" or bool(item.get("request_compact", False))
            directive_continue_xml = str(item.get("continue_xml", "")).strip()
            agent_directives.append({
                "service_id": directive_service_id,
                "audit_state": directive_audit_state,
                "continue_xml": directive_continue_xml if directive_audit_state == "all_clear" else "",
                "request_compact": directive_request_compact,
                "request_compact_reason": str(item.get("request_compact_reason", "")).strip() if directive_request_compact else "",
                "summary": str(item.get("summary", "")).strip(),
            })
    child_goal_requests = _normalize_child_goal_requests(raw_child_goal_recs if not goal_satisfied else [])
    user_response_requests: list[dict[str, Any]] = []
    if not goal_satisfied and progress_state == "in_progress":
        for item in raw_user_response_recs:
            question = str(item.get("question") or "").strip()
            if not question:
                continue
            try:
                timeout_seconds = int(item.get("timeout_seconds", 300) or 300)
            except (TypeError, ValueError):
                timeout_seconds = 300
            user_response_requests.append(
                {
                    "question": question,
                    "reason": str(item.get("reason") or "").strip(),
                    "timeout_seconds": timeout_seconds,
                }
            )
            break
    return {
        "goal_audit_session_id": str(audit_session_id or ""),
        "goal_audit_provider_session_id": str(audit_session_id or ""),
        "goal_audit_conversation_session_id": "",
        "progress_state": progress_state,
        "audit_state": audit_state,
        "goal_satisfied": goal_satisfied,
        "summary": summary,
        "continue_xml": continue_xml if audit_state == "all_clear" and not goal_satisfied else "",
        "request_compact": request_compact,
        "request_compact_reason": request_compact_reason if request_compact else "",
        "agent_directives": agent_directives,
        "child_goal_requests": child_goal_requests,
        "user_response_requests": user_response_requests,
        "pending_turn_completed_events": pending_turn_completed_events,
        "last_reviewed_turn_completed_at": last_reviewed_turn_completed_at,
        "verified_artifacts": verified_artifacts,
        "log_bundle_path": str(log_bundle_path),
        "log_record_count": log_record_count,
    }
