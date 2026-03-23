from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .common import normalize_stream_event, schema_text


def normalize_claude_stream_event(record: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_stream_event("claude", record)
    record_type = str(record.get("type") or "")
    if record_type == "system":
        normalized["raw_type"] = record_type
        subtype = str(record.get("subtype") or "").strip()
        normalized["type"] = f"claude.system.{subtype or 'event'}"
        return normalized
    if record_type not in {"assistant", "user"}:
        return normalized

    normalized["raw_type"] = record_type
    message = record.get("message")
    if not isinstance(message, dict):
        normalized["type"] = f"claude.{record_type}"
        return normalized
    content = message.get("content")
    if not isinstance(content, list) or not content:
        normalized["type"] = f"claude.{record_type}"
        return normalized

    first = content[0]
    if not isinstance(first, dict):
        normalized["type"] = f"claude.{record_type}"
        return normalized
    content_type = str(first.get("type") or "").strip()
    if content_type == "tool_use":
        normalized["type"] = f"claude.{record_type}.tool_use"
        normalized["tool_name"] = first.get("name")
        normalized["tool_input"] = first.get("input")
        return normalized
    if content_type == "tool_result":
        normalized["type"] = f"claude.{record_type}.tool_result"
        normalized["tool_result"] = first.get("content")
        return normalized
    if content_type == "text":
        normalized["type"] = f"claude.{record_type}.text"
        normalized["text"] = first.get("text")
        return normalized

    normalized["type"] = f"claude.{record_type}.{content_type or 'message'}"
    return normalized


def run_claude(
    prompt: str,
    *,
    session_id: str | None,
    response_schema_id: str | None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[str, list[dict[str, Any]], str | None]:
    cmd = [
        "claude",
        "-p",
        "--dangerously-skip-permissions",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
    if response_schema_id:
        cmd.extend(["--json-schema", schema_text(response_schema_id)])
    cmd.append(prompt)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    events: list[dict[str, Any]] = []
    final_text = ""
    next_session_id: str | None = session_id
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        record = normalize_claude_stream_event(json.loads(line))
        events.append(record)
        if on_event is not None:
            on_event(record)
        if isinstance(record.get("structured_output"), dict):
            final_text = json.dumps(record["structured_output"], ensure_ascii=False)
        if record.get("type") == "result" and not record.get("is_error", False):
            if not final_text:
                final_text = str(record.get("result", "")).strip()
            if record.get("session_id"):
                next_session_id = str(record["session_id"])
    stderr = proc.stderr.read().strip() if proc.stderr is not None else ""
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(stderr or f"claude failed with exit code {rc}")
    return final_text, events, next_session_id


def _run_claude_helper(
    *,
    repo_root: Path,
    helper_name: str,
    failed_type: str,
    checked_type: str,
    session_id: str,
    threshold_left_percent: int,
    extra_args: list[str] | None = None,
) -> tuple[dict[str, Any], int]:
    helper = repo_root / ".temp" / helper_name
    if not helper.exists():
        return (
            {
                "type": failed_type,
                "threshold_left_percent": threshold_left_percent,
                "session_id": session_id,
                "error": "helper_missing",
                "helper_path": str(helper),
            },
            1,
        )
    cmd = [str(helper), str(session_id), str(repo_root)]
    if extra_args:
        cmd.extend(extra_args)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except Exception as exc:
        return (
            {
                "type": failed_type,
                "threshold_left_percent": threshold_left_percent,
                "session_id": session_id,
                "error": repr(exc),
            },
            1,
        )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    result: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        if ": " not in raw_line:
            continue
        key, value = raw_line.split(": ", 1)
        result[key.strip()] = value.strip()
    event: dict[str, Any] = {
        "type": checked_type,
        "threshold_left_percent": threshold_left_percent,
        "session_id": session_id,
    }
    event.update(result)
    if stderr:
        event["stderr"] = stderr
    if proc.returncode != 0:
        event["type"] = failed_type
        event["returncode"] = str(proc.returncode)
    return event, proc.returncode


def run_claude_compaction(
    *,
    repo_root: Path,
    session_id: str,
    threshold_left_percent: int,
    mode: str,
) -> tuple[dict[str, Any], int]:
    if mode == "auto":
        checked_type = "service.auto_compact_checked"
        failed_type = "service.auto_compact_failed"
    elif mode == "goal_manager":
        checked_type = "service.goal_manager_compact_checked"
        failed_type = "service.goal_manager_compact_failed"
    else:
        checked_type = "service.manual_compact_checked"
        failed_type = "service.manual_compact_failed"
    event, rc = _run_claude_helper(
        repo_root=repo_root,
        helper_name="compact_claude_session.sh",
        failed_type=failed_type,
        checked_type=checked_type,
        session_id=session_id,
        threshold_left_percent=threshold_left_percent,
        extra_args=[str(threshold_left_percent)],
    )
    event = {
        "type": event.get("type"),
        "threshold_left_percent": threshold_left_percent,
        "session_id": session_id,
        "left_percent": event.get("left_percent"),
        "used_percent": event.get("used_percent"),
        "command_status": event.get("command_status"),
        "prompt_line": event.get("prompt_line"),
        "compaction": event.get("compaction", "unknown"),
        "wait_status": event.get("wait_status"),
        "post_left_percent": event.get("post_left_percent"),
        "post_used_percent": event.get("post_used_percent"),
        **({"stderr": event["stderr"]} if "stderr" in event else {}),
        **({"returncode": event["returncode"]} if "returncode" in event else {}),
        **({"error": event["error"]} if "error" in event else {}),
        **({"helper_path": event["helper_path"]} if "helper_path" in event else {}),
    }
    return event, rc


def run_claude_context_check(
    *,
    repo_root: Path,
    session_id: str,
    threshold_left_percent: int,
) -> tuple[dict[str, Any], int]:
    checked_type = "service.auto_compact_checked"
    failed_type = "service.auto_compact_failed"
    event, rc = _run_claude_helper(
        repo_root=repo_root,
        helper_name="check_claude_context_window.sh",
        failed_type=failed_type,
        checked_type=checked_type,
        session_id=session_id,
        threshold_left_percent=threshold_left_percent,
    )
    left_percent = event.get("left_percent")
    used_percent = event.get("used_percent")
    compaction = "unknown"
    try:
        compaction = "needed" if int(str(left_percent)) <= int(threshold_left_percent) else "not_needed"
    except (TypeError, ValueError):
        compaction = "unknown"
    normalized_event: dict[str, Any] = {
        "type": event.get("type"),
        "threshold_left_percent": threshold_left_percent,
        "session_id": session_id,
        "left_percent": left_percent,
        "used_percent": used_percent,
        "compaction": compaction,
        "status_line": event.get("status_line"),
    }
    if "stderr" in event:
        normalized_event["stderr"] = event["stderr"]
    if "returncode" in event:
        normalized_event["returncode"] = event["returncode"]
    if "error" in event:
        normalized_event["error"] = event["error"]
    if "helper_path" in event:
        normalized_event["helper_path"] = event["helper_path"]
    return normalized_event, rc
