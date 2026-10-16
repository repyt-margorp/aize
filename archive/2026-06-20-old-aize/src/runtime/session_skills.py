from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any, Callable

from runtime.persistent_state_pkg import session_skill_file_path
from wire.protocol import utc_ts


SESSION_SKILL_AGENT_PROVIDER = "session_skill"


def _service_id_for_skill(skill: dict[str, Any]) -> str:
    raw_skill_id = str(skill.get("skill_id") or "session-skill").strip() or "session-skill"
    safe_skill_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw_skill_id).strip("-") or "session-skill"
    return f"session-skill-{safe_skill_id}"


def _skill_scope(skill: dict[str, Any]) -> str:
    value = str(skill.get("skill_scope") or "").strip().lower()
    if value in {"unit", "adaptive"}:
        return value
    if value == "template":
        return "unit"
    return ""


def _skill_tags_match(skill: dict[str, Any], *, prompt_text: str) -> bool:
    tags = [
        str(tag).strip().lower()
        for tag in skill.get("routing_tags", [])
        if str(tag).strip()
    ]
    if not tags:
        return True
    normalized_prompt = " ".join(str(prompt_text or "").strip().lower().split())
    return any(tag in normalized_prompt for tag in tags)


def matching_interactive_session_skills(
    session: dict[str, Any] | None,
    *,
    prompt_text: str,
) -> list[dict[str, Any]]:
    record = session if isinstance(session, dict) else {}
    matched: list[dict[str, Any]] = []
    for skill in record.get("session_skills", []) if isinstance(record.get("session_skills"), list) else []:
        if not isinstance(skill, dict):
            continue
        routing_mode = str(skill.get("routing_mode") or "").strip().lower()
        kind = str(skill.get("kind") or "").strip().lower()
        handler_path = str(
            skill.get("handler_file")
            or skill.get("entrypoint")
            or skill.get("handler")
            or ""
        ).strip()
        if not handler_path:
            continue
        if routing_mode not in {"handle_user_message", "interactive_skill"} and kind not in {
            "interactive",
            "interactive_skill",
            "communication",
            "message_handler",
        }:
            continue
        if not _skill_tags_match(skill, prompt_text=prompt_text):
            continue
        matched.append(skill)
    return matched


def run_interactive_session_skill(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    skill: dict[str, Any],
    prompt_text: str,
    session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    handler_relative_path = str(
        skill.get("handler_file")
        or skill.get("entrypoint")
        or skill.get("handler")
        or ""
    ).strip()
    if not handler_relative_path:
        raise RuntimeError("session skill handler_file is required")
    handler_path = session_skill_file_path(
        runtime_root,
        username=username,
        session_id=session_id,
        relative_path=handler_relative_path,
    )
    if not handler_path.exists():
        raise RuntimeError(f"session skill handler not found: {handler_relative_path}")

    module_name_suffix = re.sub(r"[^A-Za-z0-9_]+", "_", str(skill.get("skill_id") or "skill")).strip("_") or "skill"
    module_name = f"aize_session_skill_{session_id}_{module_name_suffix}"
    spec = importlib.util.spec_from_file_location(module_name, handler_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"session skill handler could not be loaded: {handler_relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handle = getattr(module, "handle", None)
    if not callable(handle):
        raise RuntimeError(f"session skill handler has no callable handle(): {handler_relative_path}")
    result = handle(
        {
            "username": username,
            "session_id": session_id,
            "prompt_text": prompt_text,
            "skill": dict(skill),
            "session": dict(session or {}),
        }
    )
    if isinstance(result, str):
        return {"assistant_text": result}
    if isinstance(result, dict):
        assistant_text = str(result.get("assistant_text") or result.get("text") or "").strip()
        return {**result, "assistant_text": assistant_text}
    return {"assistant_text": ""}


def append_session_skill_agent_turn(
    append_history: Callable[[str, str, dict[str, Any]], None],
    *,
    username: str,
    session_id: str,
    skill: dict[str, Any],
    text: str,
    status: str = "success",
    error: str = "",
) -> None:
    service_id = _service_id_for_skill(skill)
    now = utc_ts()
    append_history(
        username,
        session_id,
        {
            "direction": "event",
            "ts": now,
            "service_id": service_id,
            "session_id": session_id,
            "event_type": "agent.turn_started",
            "text": "Session Skill started responding",
            "event": {
                "type": "agent.turn_started",
                "service_id": service_id,
                "provider": SESSION_SKILL_AGENT_PROVIDER,
                "skill_id": str(skill.get("skill_id") or ""),
                "skill_scope": _skill_scope(skill),
            },
        },
    )
    if text:
        append_history(
            username,
            session_id,
            {
                "direction": "in",
                "ts": now,
                "from": service_id,
                "service_id": service_id,
                "session_id": session_id,
                "text": text,
                "provider": SESSION_SKILL_AGENT_PROVIDER,
                "skill_id": str(skill.get("skill_id") or ""),
                "skill_scope": _skill_scope(skill),
            },
        )
    append_history(
        username,
        session_id,
        {
            "direction": "event",
            "ts": now,
            "service_id": service_id,
            "session_id": session_id,
            "event_type": "turn.completed",
            "text": "Turn completed",
            "event": {
                "type": "turn.completed",
                "status": status,
                "provider": SESSION_SKILL_AGENT_PROVIDER,
                "skill_id": str(skill.get("skill_id") or ""),
                "skill_scope": _skill_scope(skill),
                "error": error,
            },
        },
    )
