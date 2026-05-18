from __future__ import annotations

from typing import Any


def session_goal_completion_policy(session_settings: dict[str, Any] | None) -> str:
    return str((session_settings or {}).get("goal_completion_policy") or "standard").strip().lower()


def communication_goal_cycle_enabled(session_settings: dict[str, Any] | None) -> bool:
    settings = session_settings or {}
    return bool(settings.get("communication_agent_enabled", False)) and (
        session_goal_completion_policy(settings) == "per_prompt"
    )


def should_complete_communication_goal_after_reply(
    session_settings: dict[str, Any] | None,
    *,
    visible_text: str,
) -> bool:
    settings = session_settings or {}
    if not communication_goal_cycle_enabled(settings):
        return False
    if not str(settings.get("goal_text") or "").strip():
        return False
    if not bool(settings.get("goal_active", False)):
        return False
    return bool(str(visible_text or "").strip())


def should_preserve_prompt_cycle_progress_during_goal_review(
    session_settings: dict[str, Any] | None,
    *,
    audit_progress_state: str,
    resolved_audit_state: str,
) -> bool:
    if not communication_goal_cycle_enabled(session_settings):
        return False
    if str(audit_progress_state or "").strip().lower() == "complete":
        return False
    return str(resolved_audit_state or "").strip().lower() != "panic"
