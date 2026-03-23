from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from runtime.message_builder import build_outgoing_event_message
from runtime.service_control import extract_agent_message_visible_text
from wire.protocol import utc_ts


def summarize_provider_event(event: dict[str, Any]) -> str:
    event_type = str(event.get("type", "event"))
    if event_type == "turn.completed":
        status = str(event.get("status", "success")).strip().lower() or "success"
        if status == "failed":
            error = str(event.get("error", "")).strip()
            return f"Turn completed | failed{f': {error}' if error else ''}"
        return "Turn completed"
    if event_type == "service.progress_inquiry_requested":
        source_kind = str(event.get("source_kind", "input"))
        return f"Progress inquiry requested after queued {source_kind}"
    if event_type == "service.progress_inquiry_deferred":
        provider = str(event.get("provider", "unknown"))
        return f"Progress inquiry deferred until TurnCompleted | provider {provider}"
    if event_type == "service.progress_inquiry_completed":
        summary = str(event.get("summary", "")).strip()
        return summary or "Progress inquiry completed"
    if event_type == "service.progress_inquiry_failed":
        return "Progress inquiry failed"
    if event_type == "service.auto_compact_checked":
        left_percent = str(event.get("left_percent", "?"))
        compaction = str(event.get("compaction", "unknown"))
        return f"Context {left_percent}% left | compaction {compaction}"
    if event_type == "service.auto_compact_failed":
        left_percent = str(event.get("left_percent", "?"))
        return f"Context check failed | last left {left_percent}%"
    if event_type == "service.manual_compact_checked":
        left_percent = str(event.get("left_percent", "?"))
        compaction = str(event.get("compaction", "unknown"))
        return f"Manual compact | context {left_percent}% left | compaction {compaction}"
    if event_type == "service.manual_compact_failed":
        left_percent = str(event.get("left_percent", "?"))
        return f"Manual compact failed | last left {left_percent}%"
    if event_type == "service.goal_manager_compact_checked":
        left_percent = str(event.get("left_percent", "?"))
        compaction = str(event.get("compaction", "unknown"))
        reason = str(event.get("reason", "")).strip()
        suffix = f" | reason {reason}" if reason else ""
        return f"GoalManager compact | context {left_percent}% left | compaction {compaction}{suffix}"
    if event_type == "service.goal_manager_compact_failed":
        left_percent = str(event.get("left_percent", "?"))
        reason = str(event.get("reason", "")).strip()
        suffix = f" | reason {reason}" if reason else ""
        return f"GoalManager compact failed | last left {left_percent}%{suffix}"
    if event_type == "service.goal_audit_started":
        return "GoalManager running"
    if event_type == "service.goal_audit_completed":
        progress_state = str(event.get("progress_state", "complete" if bool(event.get("goal_satisfied")) else "in_progress"))
        audit_state = str(event.get("audit_state", "all_clear"))
        if progress_state == "complete":
            return f"GoalManager completed the goal | audit {audit_state}"
        request_compact = bool(event.get("request_compact"))
        reason = str(event.get("request_compact_reason", "")).strip()
        if request_compact and reason:
            return f"GoalManager requested more work | audit {audit_state} | compact requested: {reason}"
        if request_compact:
            return f"GoalManager requested more work | audit {audit_state} | compact requested"
        return f"GoalManager requested more work | audit {audit_state}"
    if event_type == "service.goal_audit_failed":
        return "GoalManager failed"
    if event_type == "service.goal_audit_parse_retry":
        attempt = int(event.get("attempt", 1))
        error = str(event.get("error", "")).strip()
        short_error = error[:120] if error else "unknown parse error"
        return f"GoalManager parse error (attempt {attempt}) — retrying | {short_error}"
    if event_type == "service.restart_resume_enqueued":
        previous_process_id = str(event.get("previous_process_id", "?"))
        process_id = str(event.get("process_id", "?"))
        return f"System restarted | resuming previous work ({previous_process_id} → {process_id})"
    if event_type == "claude.system.init":
        session_id = str(event.get("session_id", "")).strip()
        return f"Claude session initialized{f': {session_id}' if session_id else ''}"
    if event_type == "claude.assistant.tool_use":
        tool_name = str(event.get("tool_name", "tool")).strip() or "tool"
        return f"Claude tool use: {tool_name}"
    if event_type == "claude.user.tool_result":
        tool_result = event.get("tool_result")
        if isinstance(tool_result, list):
            parts = []
            for item in tool_result:
                if isinstance(item, dict):
                    item_text = str(item.get("text", "")).strip()
                    if item_text:
                        parts.append(item_text)
            preview = " ".join(parts).strip()
        else:
            preview = str(tool_result or "").strip()
        if preview:
            return f"Claude tool result: {preview[:120]}"
        return "Claude tool result"
    if event_type == "claude.assistant.text":
        preview = str(event.get("text", "")).strip()
        if preview:
            return f"Claude message: {preview[:120]}"
        return "Claude message"
    if event_type == "thread.started":
        return f"{event_type}: {event.get('thread_id', '')}".strip()
    if event_type == "item.completed":
        item = event.get("item")
        if isinstance(item, dict):
            if item.get("type") == "agent_message":
                visible_text = extract_agent_message_visible_text(str(item.get("text", "")))
                if visible_text:
                    return visible_text
            return f"{event_type}: {item.get('type', 'item')}"
    if event_type == "agent_message.delta":
        delta = str(event.get("delta", "")).strip()
        if delta:
            return f"{event_type}: {delta[:120]}"
    return event_type


def make_history_event_entry(event: dict[str, Any], *, service_id: str) -> dict[str, Any]:
    entry = {
        "direction": "event",
        "ts": utc_ts(),
        "service_id": service_id,
        "event_type": str(event.get("type", "event")),
        "text": summarize_provider_event(event),
        "event": event,
    }
    if event.get("left_percent") is not None:
        entry["context_left_percent"] = str(event.get("left_percent"))
    if event.get("used_percent") is not None:
        entry["context_used_percent"] = str(event.get("used_percent"))
    if event.get("post_left_percent") is not None:
        entry["context_post_left_percent"] = str(event.get("post_left_percent"))
    if event.get("post_used_percent") is not None:
        entry["context_post_used_percent"] = str(event.get("post_used_percent"))
    if event.get("compaction") is not None:
        entry["context_compaction"] = str(event.get("compaction"))
    return entry


def emit_turn_completed_event(
    *,
    runtime_root: Path,
    manifest: dict[str, Any],
    from_service_id: str,
    to_service_id: str,
    process_id: str,
    run_id: str,
    username: str | None,
    session_id: str | None,
    send_tx: Callable[[dict[str, Any]], None],
    reply_index: int,
    status: str,
    provider: str,
    error: str | None = None,
) -> None:
    if not username or not session_id:
        return
    event: dict[str, Any] = {
        "type": "turn.completed",
        "status": status,
        "reply_index": reply_index,
        "process_id": process_id,
        "provider": provider,
        "completed_at": utc_ts(),
    }
    if error:
        event["error"] = error
    entry = make_history_event_entry(event, service_id=from_service_id)
    message = build_outgoing_event_message(
        runtime_root=runtime_root,
        manifest=manifest,
        from_node_id=manifest["node_id"],
        from_service_id=from_service_id,
        to_node_id=manifest["node_id"],
        to_service_id=to_service_id,
        process_id=process_id,
        run_id=run_id,
        entry=entry,
        username=username,
        session_id=session_id,
    )
    send_tx(message)
