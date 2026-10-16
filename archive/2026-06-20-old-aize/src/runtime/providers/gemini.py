from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

from .common import normalize_stream_event, parse_stream_json_line


def _gemini_compaction_event(*, checked_type: str, session_id: str, threshold_left_percent: int, mode: str) -> dict[str, Any]:
    return {
        "type": checked_type,
        "threshold_left_percent": threshold_left_percent,
        "session_id": session_id,
        "left_percent": "?",
        "used_percent": "?",
        "command_status": "not_required",
        "prompt_line": "",
        "compaction": "skipped",
        "wait_status": f"gemini_{mode}_compact_not_required",
        "post_left_percent": "?",
        "post_used_percent": "?",
        "provider": "gemini",
    }


def run_gemini(
    prompt: str,
    *,
    session_id: str | None,
    response_schema_id: str | None,
    model: str | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[str, list[dict[str, Any]], str | None]:
    cmd = [
        "gemini",
        "--prompt",
        prompt,
        "--approval-mode",
        "yolo",
        "--output-format",
        "stream-json",
    ]
    if model:
        cmd.extend(["--model", str(model)])
    if session_id:
        cmd.extend(["--resume", session_id])
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    events: list[dict[str, Any]] = []
    final_text = ""
    message_parts: list[str] = []
    next_session_id: str | None = session_id
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        record = normalize_stream_event("gemini", parse_stream_json_line(line))
        events.append(record)
        if on_event is not None:
            on_event(record)
        if isinstance(record.get("text"), str) and record.get("text"):
            final_text = str(record.get("text")).strip()
        elif isinstance(record.get("result"), str) and record.get("result"):
            final_text = str(record.get("result")).strip()
        elif (
            str(record.get("type") or "").strip() == "message"
            and str(record.get("role") or "").strip() == "assistant"
            and isinstance(record.get("content"), str)
            and record.get("content")
        ):
            message_parts.append(str(record.get("content")))
            final_text = "".join(message_parts).strip()
        for session_key in ("session_id", "conversation_id", "thread_id"):
            session_value = record.get(session_key)
            if isinstance(session_value, str) and session_value.strip():
                next_session_id = session_value.strip()
                break
    stderr = proc.stderr.read().strip() if proc.stderr is not None else ""
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(stderr or f"gemini failed with exit code {rc}")
    return final_text, events, next_session_id


def run_gemini_compaction(
    *,
    repo_root: Any,
    session_id: str,
    threshold_left_percent: int,
    mode: str,
) -> tuple[dict[str, Any], int]:
    del repo_root
    if mode == "auto":
        checked_type = "service.auto_compact_checked"
    elif mode == "goal_manager":
        checked_type = "service.goal_manager_compact_checked"
    else:
        checked_type = "service.manual_compact_checked"
    return _gemini_compaction_event(
        checked_type=checked_type,
        session_id=session_id,
        threshold_left_percent=threshold_left_percent,
        mode=mode,
    ), 0


def run_gemini_context_check(
    *,
    repo_root: Any,
    session_id: str,
    threshold_left_percent: int,
) -> tuple[dict[str, Any], int]:
    return run_gemini_compaction(
        repo_root=repo_root,
        session_id=session_id,
        threshold_left_percent=threshold_left_percent,
        mode="auto",
    )
