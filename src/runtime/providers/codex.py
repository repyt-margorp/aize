from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .common import normalize_stream_event, parse_stream_json_line, schema_path


def _is_usage_limit_error(text: str) -> bool:
    return "you've hit your usage limit" in text.lower()


def run_codex(
    prompt: str,
    *,
    session_id: str | None,
    response_schema_id: str | None,
    model: str | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[str, list[dict[str, Any]], str | None]:
    def build_cmd(attempt_model: str | None) -> list[str]:
        if session_id:
            cmd = [
                "codex",
                "exec",
                "resume",
                "--dangerously-bypass-approvals-and-sandbox",
                "--json",
            ]
            if attempt_model:
                cmd.extend(["--model", str(attempt_model)])
            cmd.extend([session_id, prompt])
        else:
            cmd = [
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--json",
            ]
            if attempt_model:
                cmd.extend(["--model", str(attempt_model)])
            if response_schema_id:
                cmd.extend(["--output-schema", str(schema_path(response_schema_id))])
            cmd.append(prompt)
        return cmd

    models: list[str | None] = [model]
    if model is not None:
        models.append(None)
    last_error = None
    for attempt_model in models:
        attempt_cmd = build_cmd(attempt_model)
        proc = subprocess.Popen(
            attempt_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        events: list[dict[str, Any]] = []
        final_text = ""
        next_session_id = session_id
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            record = normalize_stream_event("codex", parse_stream_json_line(line))
            events.append(record)
            if on_event is not None:
                on_event(record)
            if record.get("type") == "thread.started" and record.get("thread_id"):
                next_session_id = str(record["thread_id"])
            if (
                record.get("type") == "item.completed"
                and isinstance(record.get("item"), dict)
                and record["item"].get("type") == "agent_message"
            ):
                final_text = str(record["item"].get("text", "")).strip()
        stderr = proc.stderr.read().strip() if proc.stderr is not None else ""
        rc = proc.wait()
        if rc == 0:
            return final_text, events, next_session_id

        error_text = stderr or f"codex failed with exit code {rc}"
        last_error = error_text
        if attempt_model is not None and _is_usage_limit_error(error_text):
            continue
        raise RuntimeError(error_text)
    raise RuntimeError(last_error or "codex failed with exit code 1")


def _run_codex_helper(
    *,
    repo_root: Path,
    helper_name: str,
    failed_type: str,
    checked_type: str,
    session_id: str,
    threshold_left_percent: int,
    extra_args: list[str] | None = None,
) -> tuple[dict[str, Any], int]:
    helper_candidates = [
        repo_root / ".temp" / helper_name,
        repo_root / "scripts" / helper_name,
    ]
    helper = next((candidate for candidate in helper_candidates if candidate.exists()), None)
    if helper is None:
        return (
            {
                "type": failed_type,
                "threshold_left_percent": threshold_left_percent,
                "session_id": session_id,
                "error": "helper_missing",
                "helper_path": str(helper_candidates[0]),
                "helper_candidates": [str(path) for path in helper_candidates],
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


def run_codex_compaction(
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
    event, rc = _run_codex_helper(
        repo_root=repo_root,
        helper_name="compact_codex_session.sh",
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


def run_codex_context_check(
    *,
    repo_root: Path,
    session_id: str,
    threshold_left_percent: int,
) -> tuple[dict[str, Any], int]:
    checked_type = "service.auto_compact_checked"
    failed_type = "service.auto_compact_failed"
    event, rc = _run_codex_helper(
        repo_root=repo_root,
        helper_name="check_codex_context_window.sh",
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
