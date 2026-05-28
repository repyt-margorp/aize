from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from unit_catalog import list_unit_file_descriptors
from runtime.persistent_state_pkg import (
    DEFAULT_AUTO_COMPACT_THRESHOLD_LEFT_PERCENT,
    SESSION_GROUP_DEFAULT_PERMISSIONS,
    add_session_child,
    session_template_metadata_path,
    session_templates_dir,
    unit_metadata_path,
    units_dir,
    create_child_conversation_session,
    create_conversation_session,
    ensure_session_storage_unlocked,
    ensure_session_template_workspace,
    get_session_settings,
    list_session_parents,
    list_sessions,
    normalize_child_session_sharing_policy,
    normalize_session_skills,
    normalize_username,
    read_json_file,
    state_lock,
    state_read_lock,
    update_session_goal,
    update_session_goal_flags,
    update_session_launcher_profile,
    update_session_selected_agents,
    update_session_skills,
    write_json_file,
)
from runtime.persistent_state_pkg._core import _load_state_unlocked, write_state
from wire.protocol import utc_ts

VALID_PROVIDERS = {"codex", "claude", "gemini"}
POOL_TOKENS = {"codex_pool": "codex", "claude_pool": "claude", "gemini_pool": "gemini"}
WORKSPACE_SCOPES = {"none", "unit", "app"}
SCHEDULE_KINDS = {"daily", "interval"}
SCHEDULE_RETRY_AFTER_SECONDS = 60


def _ensure_resident_parent_session(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> None:
    normalized_username = normalize_username(username)
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        return
    if get_session_settings(runtime_root, username=normalized_username, session_id=normalized_session_id):
        return
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        sessions = state.setdefault("conversation_sessions", {}).setdefault(normalized_username, [])
        if any(str((session or {}).get("session_id") or "").strip() == normalized_session_id for session in sessions):
            return
        session = {
            "session_id": normalized_session_id,
            "label": "Root",
            "session_group": "root",
            "auto_compact_threshold_left_percent": DEFAULT_AUTO_COMPACT_THRESHOLD_LEFT_PERCENT,
            "created_at": utc_ts(),
            "updated_at": utc_ts(),
            "created_by_username": normalized_username,
            "created_by_type": "user",
            "origin_session_id": "",
            "origin_goal_id": "",
            "origin_goal_text": "",
        }
        ensure_session_storage_unlocked(runtime_root, username=normalized_username, session=session)
        sessions.append(session)
        write_state(runtime_root, state)


def _read_unit_metadata(runtime_root: Path, *, username: str, template_id: str) -> dict[str, Any] | None:
    metadata_path = unit_metadata_path(runtime_root, username=username, unit_id=template_id)
    metadata = read_json_file(metadata_path)
    if isinstance(metadata, dict):
        return metadata
    legacy_metadata_path = session_template_metadata_path(runtime_root, username=username, template_id=template_id)
    legacy_metadata = read_json_file(legacy_metadata_path)
    if isinstance(legacy_metadata, dict):
        return legacy_metadata
    legacy_path = legacy_metadata_path.with_name("app.json")
    legacy = read_json_file(legacy_path)
    return legacy if isinstance(legacy, dict) else None


def _ensure_session_parent_link(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    parent_session_id: str,
) -> None:
    normalized_username = normalize_username(username)
    normalized_session_id = str(session_id or "").strip()
    normalized_parent_session_id = str(parent_session_id or "").strip()
    if (
        not normalized_session_id
        or not normalized_parent_session_id
        or normalized_session_id == normalized_parent_session_id
    ):
        return
    session = get_session_settings(
        runtime_root,
        username=normalized_username,
        session_id=normalized_session_id,
    )
    if not isinstance(session, dict):
        return
    _ensure_resident_parent_session(
        runtime_root,
        username=normalized_username,
        session_id=normalized_parent_session_id,
    )
    parents = list_session_parents(
        runtime_root,
        username=normalized_username,
        session_id=normalized_session_id,
    )
    metadata_parent_session_id = str(session.get("parent_session_id") or "").strip()
    if (
        normalized_parent_session_id in parents
        and metadata_parent_session_id == normalized_parent_session_id
    ):
        return
    add_session_child(
        runtime_root,
        username=normalized_username,
        parent_session_id=normalized_parent_session_id,
        child_session_id=normalized_session_id,
    )


def _repair_registered_template_lineage(
    runtime_root: Path,
    *,
    username: str,
    template_state: dict[str, Any] | None = None,
    session_template: dict[str, Any] | None = None,
) -> None:
    if not isinstance(template_state, dict):
        return
    normalized_username = normalize_username(username)
    normalized_template_id = str(
        template_state.get("template_id") or template_state.get("unit_id") or ""
    ).strip()
    last_session_id = str(template_state.get("last_session_id") or "").strip()
    if not normalized_template_id or not last_session_id:
        return
    launcher = dict((session_template or {}).get("launcher") or {})
    resident_parent_session_id = str(launcher.get("resident_parent_session_id") or "").strip()
    if not resident_parent_session_id:
        return
    _ensure_session_parent_link(
        runtime_root,
        username=normalized_username,
        session_id=last_session_id,
        parent_session_id=resident_parent_session_id,
    )
    if str(template_state.get("last_parent_session_id") or "").strip() != resident_parent_session_id:
        update_registered_session_template_state(
            runtime_root,
            username=normalized_username,
            template_id=normalized_template_id,
            updates={"last_parent_session_id": resident_parent_session_id},
        )


def _normalize_provider(value: Any, *, default_provider: str) -> str:
    provider = str(value or "").strip().lower()
    if provider in VALID_PROVIDERS:
        return provider
    fallback = str(default_provider or "codex").strip().lower()
    return fallback if fallback in VALID_PROVIDERS else "codex"


def _normalize_selected_agents(value: Any, *, preferred_provider: str) -> list[str]:
    if not isinstance(value, list):
        return [f"{preferred_provider}_pool"]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        agent = str(item or "").strip()
        if not agent or agent in seen:
            continue
        seen.add(agent)
        normalized.append(agent)
    return normalized or [f"{preferred_provider}_pool"]


def _service_targets(selected_agents: list[str], *, preferred_provider: str) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    for agent in selected_agents:
        provider = POOL_TOKENS.get(agent)
        if provider:
            targets.append({"mode": "pool", "provider": provider, "target": agent})
            continue
        inferred_provider = next((kind for kind in VALID_PROVIDERS if f"-{kind}-" in agent or agent.startswith(f"service-{kind}")), preferred_provider)
        targets.append({"mode": "service", "provider": inferred_provider, "target": agent})
    return targets or [{"mode": "pool", "provider": preferred_provider, "target": f"{preferred_provider}_pool"}]


def _normalize_workspace_scope(value: Any) -> str:
    scope = str(value or "").strip().lower()
    if scope == "app":
        return "unit"
    return scope if scope in WORKSPACE_SCOPES else "none"


def _parse_utc_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError:
        return None


def _utc_ts_for(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_schedule_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in SCHEDULE_KINDS else "daily"


def _normalize_schedule_timezone(value: Any) -> str:
    requested = str(value or "").strip() or "UTC"
    try:
        ZoneInfo(requested)
    except ZoneInfoNotFoundError:
        return "UTC"
    return requested


def _normalize_daily_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "00:00"
    hour_text, separator, minute_text = text.partition(":")
    if separator != ":":
        return "00:00"
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        return "00:00"
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return "00:00"
    return f"{hour:02d}:{minute:02d}"


def _normalize_schedule_every_hours(value: Any) -> int:
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return 24
    if hours < 1:
        return 1
    if hours > 24 * 30:
        return 24 * 30
    return hours


def _normalize_schedule(value: Any) -> dict[str, Any]:
    schedule = dict(value or {}) if isinstance(value, dict) else {}
    enabled = bool(schedule.get("enabled", bool(schedule)))
    kind = _normalize_schedule_kind(schedule.get("kind"))
    timezone = _normalize_schedule_timezone(schedule.get("timezone"))
    daily_time = _normalize_daily_time(schedule.get("daily_time") or schedule.get("time"))
    normalized = {
        "enabled": enabled,
        "kind": kind,
        "timezone": timezone,
        "daily_time": daily_time,
    }
    if kind == "interval":
        every_hours = _normalize_schedule_every_hours(
            schedule.get("every_hours") or schedule.get("interval_hours") or schedule.get("hours")
        )
        normalized["every_hours"] = every_hours
        normalized["summary"] = f"every {every_hours}h"
    else:
        normalized["summary"] = f"daily {daily_time} {timezone}"
    return normalized


def _normalize_provider_priority(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        provider = str(item or "").strip().lower()
        if provider not in VALID_PROVIDERS or provider in seen:
            continue
        seen.add(provider)
        normalized.append(provider)
    return normalized


def _normalize_provider_profile_priority(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    normalized: list[Any] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            provider = str(item.get("provider") or item.get("kind") or "").strip().lower()
            if provider not in VALID_PROVIDERS:
                continue
            entry = dict(item)
            entry["provider"] = provider
            for slot_key in ("session_slot", "lot"):
                slot_text = str(item.get(slot_key) or "").strip().lower()
                if slot_text:
                    entry["session_slot"] = slot_text
                    break
        else:
            provider = str(item or "").strip().lower()
            if provider not in VALID_PROVIDERS:
                continue
            entry = provider
        identity = str(entry)
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append(entry)
    return normalized


def _schedule_occurrence_windows(schedule: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    timezone_name = str(schedule.get("timezone") or "UTC").strip() or "UTC"
    local_zone = ZoneInfo(timezone_name)
    local_now = now.astimezone(local_zone)
    hour_text, _separator, minute_text = str(schedule.get("daily_time") or "00:00").partition(":")
    hour = int(hour_text or 0)
    minute = int(minute_text or 0)
    current_window_local = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local_now < current_window_local:
        current_window_local = current_window_local - timedelta(days=1)
    next_window_local = current_window_local + timedelta(days=1)
    current_window_utc = current_window_local.astimezone(UTC)
    next_window_utc = next_window_local.astimezone(UTC)
    return {
        "scheduled_for_utc": _utc_ts_for(current_window_utc),
        "scheduled_for_local": current_window_local.strftime("%Y-%m-%d %H:%M"),
        "scheduled_for_local_iso": current_window_local.replace(microsecond=0).isoformat(),
        "next_due_at": _utc_ts_for(next_window_utc),
    }


def _interval_occurrence_windows(
    schedule: dict[str, Any],
    *,
    now: datetime,
    template_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    every_hours = _normalize_schedule_every_hours(
        schedule.get("every_hours") or schedule.get("interval_hours") or schedule.get("hours")
    )
    interval = timedelta(hours=every_hours)
    schedule_state = dict((template_state or {}).get("schedule_state") or {})
    anchor = (
        _parse_utc_ts(schedule_state.get("last_triggered_occurrence_at"))
        or _parse_utc_ts(schedule_state.get("last_launched_at"))
        or _parse_utc_ts((template_state or {}).get("created_at"))
        or now
    )
    scheduled_for = (anchor + interval).astimezone(UTC).replace(microsecond=0)
    while scheduled_for + interval <= now:
        scheduled_for += interval
    next_due_at = scheduled_for + interval if scheduled_for <= now else scheduled_for
    return {
        "scheduled_for_utc": _utc_ts_for(scheduled_for),
        "scheduled_for_local": scheduled_for.strftime("%Y-%m-%d %H:%M"),
        "scheduled_for_local_iso": scheduled_for.isoformat(),
        "next_due_at": _utc_ts_for(next_due_at),
    }


def list_registered_session_template_states(runtime_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    with state_read_lock(runtime_root):
        for root in (units_dir(runtime_root), session_templates_dir(runtime_root)):
            if not root.exists():
                continue
            for user_dir in sorted(path for path in root.iterdir() if path.is_dir()):
                username = normalize_username(user_dir.name)
                for unit_dir_entry in sorted(path for path in user_dir.iterdir() if path.is_dir()):
                    key = (username, unit_dir_entry.name)
                    if key in seen:
                        continue
                    metadata = _read_unit_metadata(runtime_root, username=username, template_id=unit_dir_entry.name)
                    if not isinstance(metadata, dict):
                        continue
                    seen.add(key)
                    record = dict(metadata)
                    record["username"] = username
                    record["unit_id"] = str(record.get("unit_id") or record.get("template_id") or unit_dir_entry.name).strip() or unit_dir_entry.name
                    record["template_id"] = record["unit_id"]
                    records.append(record)
    return records


def get_registered_session_template_state(runtime_root: Path, *, username: str, template_id: str) -> dict[str, Any] | None:
    normalized_username = normalize_username(username)
    normalized_template_id = str(template_id or "").strip()
    with state_read_lock(runtime_root):
        metadata = _read_unit_metadata(runtime_root, username=normalized_username, template_id=normalized_template_id)
    if not isinstance(metadata, dict):
        return None
    record = dict(metadata)
    record["username"] = normalized_username
    record["unit_id"] = normalized_template_id
    record["template_id"] = normalized_template_id
    return record


def update_registered_session_template_state(
    runtime_root: Path,
    *,
    username: str,
    template_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    normalized_username = normalize_username(username)
    normalized_template_id = str(template_id or "").strip()
    with state_lock(runtime_root):
        metadata_path = unit_metadata_path(runtime_root, username=normalized_username, unit_id=normalized_template_id)
        current = _read_unit_metadata(runtime_root, username=normalized_username, template_id=normalized_template_id) or {}
        payload = dict(current)
        payload.update(
            {
                "unit_id": normalized_template_id,
                "template_id": normalized_template_id,
                "username": normalized_username,
            }
        )
        for key, value in dict(updates or {}).items():
            if key == "schedule_state" and isinstance(value, dict):
                nested = dict(payload.get("schedule_state") or {})
                nested.update(value)
                payload["schedule_state"] = nested
                continue
            payload[key] = value
        payload["created_at"] = str(payload.get("created_at") or utc_ts())
        payload["updated_at"] = utc_ts()
        write_json_file(metadata_path, payload)
        return dict(payload)


def describe_session_template_schedule(
    session_template: dict[str, Any],
    *,
    template_state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    schedule = dict(((session_template.get("launcher") or {}).get("schedule")) or {})
    effective_now = (now or datetime.now(UTC)).astimezone(UTC)
    schedule_state = dict((template_state or {}).get("schedule_state") or {})
    descriptor = {
        **schedule,
        "due": False,
        "unit_registered": bool(template_state),
        "template_registered": bool(template_state),
        "app_registered": bool(template_state),
        "retry_after_seconds": SCHEDULE_RETRY_AFTER_SECONDS,
        "last_triggered_occurrence_at": str(schedule_state.get("last_triggered_occurrence_at") or "").strip(),
        "last_launched_at": str(schedule_state.get("last_launched_at") or "").strip(),
        "last_launched_session_id": str(schedule_state.get("last_launched_session_id") or "").strip(),
        "retry_not_before_at": str(schedule_state.get("retry_not_before_at") or "").strip(),
        "last_error": str(schedule_state.get("last_error") or "").strip(),
        "scheduled_for_utc": "",
        "scheduled_for_local": "",
        "scheduled_for_local_iso": "",
        "next_due_at": "",
    }
    if not bool(schedule.get("enabled", False)):
        return descriptor
    if str(schedule.get("kind") or "").strip().lower() == "interval":
        window = _interval_occurrence_windows(schedule, now=effective_now, template_state=template_state)
    else:
        window = _schedule_occurrence_windows(schedule, now=effective_now)
    descriptor.update(window)
    last_triggered_at = _parse_utc_ts(schedule_state.get("last_triggered_occurrence_at"))
    retry_not_before_at = _parse_utc_ts(schedule_state.get("retry_not_before_at"))
    scheduled_for_utc = _parse_utc_ts(window["scheduled_for_utc"])
    if scheduled_for_utc is None:
        return descriptor
    if retry_not_before_at is not None and retry_not_before_at > effective_now:
        return descriptor
    if last_triggered_at is not None and last_triggered_at >= scheduled_for_utc:
        return descriptor
    descriptor["due"] = scheduled_for_utc <= effective_now
    return descriptor


def describe_app_schedule(
    app: dict[str, Any],
    *,
    app_state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    return describe_session_template_schedule(app, template_state=app_state, now=now)


def describe_unit_schedule(
    unit: dict[str, Any],
    *,
    unit_state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    return describe_session_template_schedule(unit, template_state=unit_state, now=now)


def resolve_session_template_launch_parent_session_id(
    runtime_root: Path,
    *,
    username: str,
    template_state: dict[str, Any] | None = None,
    session_template: dict[str, Any] | None = None,
) -> str | None:
    normalized_username = normalize_username(username)
    _repair_registered_template_lineage(
        runtime_root,
        username=normalized_username,
        template_state=template_state,
        session_template=session_template,
    )
    candidates: list[str] = []
    launcher = dict((session_template or {}).get("launcher") or {})
    resident_parent_session_id = str(launcher.get("resident_parent_session_id") or "").strip()
    if resident_parent_session_id:
        _ensure_resident_parent_session(
            runtime_root,
            username=normalized_username,
            session_id=resident_parent_session_id,
        )
        candidates.append(resident_parent_session_id)
    parent_unit_id = str(
        launcher.get("parent_unit_id")
        or launcher.get("parent_template_id")
        or (session_template or {}).get("parent_unit_id")
        or ""
    ).strip()
    if parent_unit_id:
        parent_state = _read_unit_metadata(
            runtime_root,
            username=normalized_username,
            template_id=parent_unit_id,
        )
        if isinstance(parent_state, dict):
            parent_last_session_id = str(parent_state.get("last_session_id") or "").strip()
            if parent_last_session_id:
                candidates.append(parent_last_session_id)
    if isinstance(template_state, dict):
        previous_parent_session_id = str(template_state.get("last_parent_session_id") or "").strip()
        if previous_parent_session_id:
            candidates.append(previous_parent_session_id)
    candidates.append("default")
    for candidate in candidates:
        if not candidate:
            continue
        session = get_session_settings(runtime_root, username=normalized_username, session_id=candidate)
        if isinstance(session, dict):
            return candidate
    sessions = list_sessions(runtime_root, username=normalized_username)
    if not sessions:
        return None
    return str(sessions[0].get("session_id") or "").strip() or None


def resolve_app_launch_parent_session_id(
    runtime_root: Path,
    *,
    username: str,
    app_state: dict[str, Any] | None = None,
) -> str | None:
    return resolve_session_template_launch_parent_session_id(
        runtime_root,
        username=username,
        template_state=app_state,
        session_template=app_state,
    )


def resolve_unit_launch_parent_session_id(
    runtime_root: Path,
    *,
    username: str,
    unit_state: dict[str, Any] | None = None,
    unit: dict[str, Any] | None = None,
) -> str | None:
    return resolve_session_template_launch_parent_session_id(
        runtime_root,
        username=username,
        template_state=unit_state,
        session_template=unit,
    )


def build_scheduled_session_template_session_label(session_template: dict[str, Any], schedule_info: dict[str, Any]) -> str:
    display_name = str(session_template.get("display_name") or session_template.get("unit_id") or session_template.get("template_id") or "Unit").strip() or "Unit"
    local_when = str(schedule_info.get("scheduled_for_local") or "").strip()
    timezone_name = str(schedule_info.get("timezone") or "UTC").strip() or "UTC"
    if not local_when:
        return f"{display_name} Scheduled Run"
    return f"{display_name} {local_when} {timezone_name}"


def build_scheduled_app_session_label(app: dict[str, Any], schedule_info: dict[str, Any]) -> str:
    return build_scheduled_session_template_session_label(app, schedule_info)


def build_scheduled_unit_session_label(unit: dict[str, Any], schedule_info: dict[str, Any]) -> str:
    return build_scheduled_session_template_session_label(unit, schedule_info)


def build_scheduled_session_template_initial_prompt(session_template: dict[str, Any], schedule_info: dict[str, Any]) -> str:
    launcher = dict(session_template.get("launcher") or {})
    base_prompt = str(launcher.get("initial_prompt") or "").strip()
    lines = [
        "<aize_scheduled_unit_launch>",
        f"  <unit_id>{session_template.get('unit_id') or session_template.get('template_id') or ''}</unit_id>",
        f"  <scheduled_for_utc>{schedule_info.get('scheduled_for_utc') or ''}</scheduled_for_utc>",
        f"  <scheduled_for_local timezone=\"{schedule_info.get('timezone') or 'UTC'}\">{schedule_info.get('scheduled_for_local') or ''}</scheduled_for_local>",
        "  <instruction>This session was created automatically from the unit's wall-clock schedule. Treat this as a fresh run, execute the unit goal now, and use the unit workspace for durable state instead of relying on prior session-local context.</instruction>",
        "</aize_scheduled_unit_launch>",
    ]
    schedule_prompt = "\n".join(lines).strip()
    return f"{schedule_prompt}\n\n{base_prompt}".strip() if base_prompt else schedule_prompt


def build_scheduled_app_initial_prompt(app: dict[str, Any], schedule_info: dict[str, Any]) -> str:
    return build_scheduled_session_template_initial_prompt(app, schedule_info)


def build_scheduled_unit_initial_prompt(unit: dict[str, Any], schedule_info: dict[str, Any]) -> str:
    return build_scheduled_session_template_initial_prompt(unit, schedule_info)


def normalize_session_template_descriptor(descriptor: dict[str, Any], *, default_provider: str) -> dict[str, Any]:
    template_id = str(descriptor.get("unit_id") or descriptor.get("template_id") or "").strip()
    if not template_id:
        source = descriptor.get("_descriptor_path", "<unknown>")
        raise RuntimeError(f"unit descriptor missing unit_id: {source}")
    launcher = dict(descriptor.get("launcher") or {})
    preferred_provider = _normalize_provider(launcher.get("preferred_provider"), default_provider=default_provider)
    selected_agents = _normalize_selected_agents(launcher.get("selected_agents"), preferred_provider=preferred_provider)
    session_group = str(launcher.get("session_group") or "user").strip().lower() or "user"
    if session_group not in SESSION_GROUP_DEFAULT_PERMISSIONS:
        session_group = "user"
    raw_permissions = launcher.get("session_permissions")
    session_permissions = dict(raw_permissions) if isinstance(raw_permissions, dict) else dict(
        SESSION_GROUP_DEFAULT_PERMISSIONS[session_group]
    )
    initial_prompt = str(launcher.get("initial_prompt") or "").strip()
    goal_text = str(launcher.get("goal_text") or "").strip()
    session_ui_mode = str(launcher.get("session_ui_mode") or descriptor.get("session_ui_mode") or "standard").strip().lower() or "standard"
    session_interactive = bool(launcher.get("session_interactive", descriptor.get("session_interactive", session_ui_mode == "communication")))
    goal_completion_policy = str(launcher.get("goal_completion_policy") or descriptor.get("goal_completion_policy") or "standard").strip().lower()
    if goal_completion_policy not in {"standard", "continuous", "per_prompt"}:
        goal_completion_policy = "standard"
    communication = dict(descriptor.get("communication") or {})
    communication_agent_enabled = bool(
        launcher.get(
            "communication_agent_enabled",
            communication.get("enabled", session_interactive),
        )
    )
    communication_agent_priority = _normalize_provider_profile_priority(
        launcher.get("communication_agent_priority") or communication.get("agent_priority")
    )
    session_skills = normalize_session_skills(launcher.get("skills"))
    if communication_agent_enabled:
        for priority_item in communication_agent_priority:
            if isinstance(priority_item, dict) and not str(priority_item.get("session_slot") or "").strip():
                priority_item["session_slot"] = "interactive_agent"
    default_label = str(launcher.get("default_label") or descriptor.get("display_name") or template_id).strip() or template_id
    unit_kind = str(descriptor.get("unit_kind") or descriptor.get("kind") or "session").strip().lower() or "session"
    instance_policy = str(descriptor.get("instance_policy") or launcher.get("instance_policy") or "multi").strip().lower() or "multi"
    interfaces = dict(descriptor.get("interfaces") or {})
    if str(launcher.get("ui_url") or "").strip() and "web" not in interfaces:
        interfaces["web"] = str(launcher.get("ui_url") or "").strip()
    normalized = {
        "unit_id": template_id,
        "template_id": template_id,
        "package_id": str(descriptor.get("package_id") or descriptor.get("plugin_id") or "").strip(),
        "plugin_id": str(descriptor.get("plugin_id") or "").strip(),
        "unit_kind": unit_kind,
        "kind": unit_kind,
        "unit_class": str(descriptor.get("unit_class") or descriptor.get("class") or ("service" if instance_policy == "singleton" else "template")).strip().lower(),
        "instance_policy": instance_policy,
        "lifecycle": str(descriptor.get("lifecycle") or launcher.get("lifecycle") or "manual").strip().lower() or "manual",
        "restart_policy": str(descriptor.get("restart_policy") or launcher.get("restart_policy") or "never").strip().lower() or "never",
        "interfaces": interfaces,
        "endpoints": dict(descriptor.get("endpoints") or {}),
        "communication": dict(descriptor.get("communication") or {}),
        "display_name": str(descriptor.get("display_name") or template_id).strip() or template_id,
        "description": str(descriptor.get("description") or "").strip(),
        "enabled": bool(descriptor.get("enabled", True)),
        "launcher": {
            "mode": "create_child_session" if bool(launcher.get("create_as_child", True)) else "create_session",
            "parent_unit_id": str(launcher.get("parent_unit_id") or launcher.get("parent_template_id") or "").strip(),
            "resident_parent_session_id": str(launcher.get("resident_parent_session_id") or "").strip(),
            "default_label": default_label,
            "goal_text": goal_text,
            "initial_prompt": initial_prompt,
            "preferred_provider": preferred_provider,
            "selected_agents": selected_agents,
            "service_targets": _service_targets(selected_agents, preferred_provider=preferred_provider),
            "session_group": session_group,
            "session_ui_mode": session_ui_mode,
            "session_interactive": session_interactive,
            "communication_agent_enabled": communication_agent_enabled,
            "communication_agent_priority": communication_agent_priority,
            "goal_completion_policy": goal_completion_policy,
            "skills": session_skills,
            "session_permissions": session_permissions,
            "child_session_sharing": normalize_child_session_sharing_policy(launcher.get("child_session_sharing")),
            "workspace_scope": _normalize_workspace_scope(launcher.get("workspace_scope")),
            "ui_url": str(launcher.get("ui_url") or interfaces.get("web") or "").strip(),
            "auto_select_session": bool(launcher.get("auto_select_session", True)),
            "auto_send_initial_prompt": bool(launcher.get("auto_send_initial_prompt", bool(initial_prompt))),
            "schedule": _normalize_schedule(launcher.get("schedule")),
        },
    }
    return normalized


def normalize_unit_descriptor(descriptor: dict[str, Any], *, default_provider: str) -> dict[str, Any]:
    return normalize_session_template_descriptor(descriptor, default_provider=default_provider)


def list_launchable_session_templates(
    *,
    default_provider: str,
    include_private: bool = True,
) -> list[dict[str, Any]]:
    apps = [
        normalize_session_template_descriptor(descriptor, default_provider=default_provider)
        for descriptor in list_unit_file_descriptors()
        if bool(descriptor.get("enabled", True))
        and (
            include_private
            or str(descriptor.get("catalog_visibility") or "public").strip().lower() == "public"
        )
    ]
    apps.sort(key=lambda item: (str(item.get("plugin_id") or ""), str(item.get("display_name") or "")))
    return apps


def list_launchable_units(*, default_provider: str, include_private: bool = True) -> list[dict[str, Any]]:
    return list_launchable_session_templates(
        default_provider=default_provider,
        include_private=include_private,
    )


def ensure_auto_scheduled_root_unit_states(
    runtime_root: Path,
    *,
    default_provider: str,
    username: str = "root",
) -> list[dict[str, Any]]:
    """Register built-in root scheduled Units so the scheduler can launch them.

    Manual Units become registered when a user first launches them. System Units
    with ``lifecycle: auto`` need state before their first scheduled occurrence,
    otherwise the schedule watcher has nothing to inspect.
    """
    normalized_username = normalize_username(username)
    # This also provisions the canonical "default" Root session if it does not
    # exist yet, giving scheduled root Units a lineage parent.
    list_sessions(runtime_root, username=normalized_username)
    registered: list[dict[str, Any]] = []
    for app in list_launchable_session_templates(default_provider=default_provider):
        launcher = dict(app.get("launcher") or {})
        schedule = dict(launcher.get("schedule") or {})
        if str(app.get("lifecycle") or "").strip().lower() != "auto":
            continue
        if str(launcher.get("session_group") or "").strip().lower() != "root":
            continue
        if not bool(schedule.get("enabled", False)):
            continue
        template_id = str(app.get("unit_id") or app.get("template_id") or "").strip()
        if not template_id:
            continue
        existing = get_registered_session_template_state(
            runtime_root,
            username=normalized_username,
            template_id=template_id,
        )
        if isinstance(existing, dict):
            registered.append(existing)
            continue
        registered.append(
            update_registered_session_template_state(
                runtime_root,
                username=normalized_username,
                template_id=template_id,
                updates={
                    "unit_id": template_id,
                    "display_name": str(app.get("display_name") or template_id).strip() or template_id,
                    "package_id": str(app.get("package_id") or app.get("plugin_id") or "").strip(),
                    "plugin_id": str(app.get("plugin_id") or "").strip(),
                    "last_parent_session_id": "default",
                    "schedule_state": {},
                },
            )
        )
    return registered


def list_registered_unit_states(runtime_root: Path) -> list[dict[str, Any]]:
    return list_registered_session_template_states(runtime_root)


def get_launchable_session_template(template_id: str, *, default_provider: str) -> dict[str, Any]:
    normalized_template_id = str(template_id or "").strip()
    for app in list_launchable_session_templates(default_provider=default_provider):
        if app["template_id"] == normalized_template_id:
            return app
    raise KeyError(normalized_template_id)


def get_launchable_unit(unit_id: str, *, default_provider: str) -> dict[str, Any]:
    return get_launchable_session_template(unit_id, default_provider=default_provider)


def get_registered_unit_state(runtime_root: Path, *, username: str, unit_id: str) -> dict[str, Any] | None:
    return get_registered_session_template_state(runtime_root, username=username, template_id=unit_id)


def update_registered_unit_state(
    runtime_root: Path,
    *,
    username: str,
    unit_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    return update_registered_session_template_state(
        runtime_root,
        username=username,
        template_id=unit_id,
        updates=updates,
    )


def launch_session_template(
    runtime_root,
    *,
    username: str,
    parent_session_id: str,
    app: dict[str, Any],
    label: str | None = None,
    goal_text: str | None = None,
    initial_prompt: str | None = None,
    preferred_provider: str | None = None,
    selected_agents: list[str] | None = None,
    origin_session_id: str | None = None,
) -> dict[str, Any]:
    launcher = dict(app.get("launcher") or {})
    normalized_username = normalize_username(username)
    effective_provider = _normalize_provider(
        preferred_provider if preferred_provider is not None else launcher.get("preferred_provider"),
        default_provider=str(launcher.get("preferred_provider") or "codex"),
    )
    effective_agents = _normalize_selected_agents(
        selected_agents if selected_agents is not None else launcher.get("selected_agents"),
        preferred_provider=effective_provider,
    )
    effective_label = str(label or launcher.get("default_label") or app.get("display_name") or app.get("template_id") or "").strip()
    effective_goal_text = str(goal_text if goal_text is not None else launcher.get("goal_text") or "").strip()
    effective_initial_prompt = str(initial_prompt if initial_prompt is not None else launcher.get("initial_prompt") or "").strip()
    effective_origin_session_id = str(origin_session_id or parent_session_id or "").strip()
    resident_parent_session_id = str(launcher.get("resident_parent_session_id") or "").strip()
    if resident_parent_session_id:
        _ensure_resident_parent_session(
            runtime_root,
            username=normalized_username,
            session_id=resident_parent_session_id,
        )
        resident_parent = get_session_settings(
            runtime_root,
            username=normalized_username,
            session_id=resident_parent_session_id,
        )
        if isinstance(resident_parent, dict):
            parent_session_id = resident_parent_session_id
    parent_unit_id = str(launcher.get("parent_unit_id") or launcher.get("parent_template_id") or "").strip()
    if parent_unit_id:
        parent_state = _read_unit_metadata(
            runtime_root,
            username=normalized_username,
            template_id=parent_unit_id,
        )
        if isinstance(parent_state, dict):
            parent_last_session_id = str(parent_state.get("last_session_id") or "").strip()
            if parent_last_session_id and get_session_settings(
                runtime_root,
                username=normalized_username,
                session_id=parent_last_session_id,
            ):
                parent_session_id = parent_last_session_id
    mode = str(launcher.get("mode") or "create_child_session").strip().lower()
    session_group = str(launcher.get("session_group") or "user").strip().lower() or "user"
    session_ui_mode = str(launcher.get("session_ui_mode") or "standard").strip().lower() or "standard"
    session_interactive = bool(launcher.get("session_interactive", session_ui_mode == "communication"))
    communication_agent_enabled = bool(launcher.get("communication_agent_enabled", session_interactive))
    communication_agent_priority = _normalize_provider_profile_priority(launcher.get("communication_agent_priority"))
    session_permissions = dict(launcher.get("session_permissions") or {})
    child_session_sharing = dict(launcher.get("child_session_sharing") or {})
    session_skills = normalize_session_skills(launcher.get("skills"))
    workspace_scope = _normalize_workspace_scope(launcher.get("workspace_scope"))
    goal_completion_policy = str(launcher.get("goal_completion_policy") or "standard").strip().lower()
    if goal_completion_policy not in {"standard", "continuous", "per_prompt"}:
        goal_completion_policy = "standard"

    if mode == "create_session":
        session = create_conversation_session(
            runtime_root,
            username=normalized_username,
            label=effective_label,
            session_group=session_group,
            session_permissions=session_permissions,
            created_by_username=normalized_username,
            created_by_type="unit",
            origin_session_id=effective_origin_session_id,
            session_ui_mode=session_ui_mode,
            session_interactive=session_interactive,
            communication_agent_enabled=communication_agent_enabled,
            communication_agent_priority=communication_agent_priority,
            child_session_sharing=child_session_sharing,
            session_skills=session_skills,
        )
        if effective_goal_text:
            session = update_session_goal(
                runtime_root,
                username=normalized_username,
                session_id=str(session["session_id"]),
                goal_text=effective_goal_text,
                updated_by_username=normalized_username,
                updated_by_type="unit",
                origin_session_id=effective_origin_session_id,
            ) or session
    else:
        session = create_child_conversation_session(
            runtime_root,
            username=normalized_username,
            parent_session_id=parent_session_id,
            label=effective_label,
            goal_text=effective_goal_text,
            session_group=session_group,
            session_permissions=session_permissions,
            created_by_username=normalized_username,
            created_by_type="unit",
            origin_session_id=effective_origin_session_id,
            session_ui_mode=session_ui_mode,
            session_interactive=session_interactive,
            communication_agent_enabled=communication_agent_enabled,
            communication_agent_priority=communication_agent_priority,
            child_session_sharing=child_session_sharing,
            session_skills=session_skills,
            requester_template_id=str(app.get("template_id") or app.get("unit_id") or "").strip(),
        )
        if not session:
            raise RuntimeError("parent_session_not_found")

    session_id = str(session.get("session_id") or "").strip()
    if not session_id:
        raise RuntimeError("session_launch_failed")
    workspace_path = ""
    if workspace_scope == "unit":
        workspace_path = str(
            ensure_session_template_workspace(
                runtime_root,
                username=normalized_username,
                template_id=str(app.get("template_id") or ""),
                display_name=str(app.get("display_name") or ""),
                plugin_id=str(app.get("plugin_id") or ""),
                session_id=session_id,
            )
        )
        workspace_note = (
            "Persistent unit workspace directory: "
            f"{workspace_path}\n"
            "Use this directory for durable code, scripts, notes, and stock that should survive across launches of this unit."
        )
        effective_initial_prompt = (
            f"{workspace_note}\n\n{effective_initial_prompt}" if effective_initial_prompt else workspace_note
        )
    update_registered_session_template_state(
        runtime_root,
        username=normalized_username,
        template_id=str(app.get("template_id") or ""),
        updates={
            "unit_id": str(app.get("unit_id") or app.get("template_id") or "").strip(),
            "display_name": str(app.get("display_name") or app.get("unit_id") or app.get("template_id") or "").strip(),
            "package_id": str(app.get("package_id") or app.get("plugin_id") or "").strip(),
            "plugin_id": str(app.get("plugin_id") or "").strip(),
            "workspace_path": workspace_path,
            "last_session_id": session_id,
            "last_parent_session_id": str(parent_session_id or "").strip(),
        },
    )
    update_session_goal_flags(
        runtime_root,
        username=normalized_username,
        session_id=session_id,
        preferred_provider=effective_provider,
    )
    update_session_selected_agents(
        runtime_root,
        username=normalized_username,
        session_id=session_id,
        selected_agents=effective_agents,
    )
    update_session_skills(
        runtime_root,
        username=normalized_username,
        session_id=session_id,
        session_skills=session_skills,
    )
    update_session_launcher_profile(
        runtime_root,
        username=normalized_username,
        session_id=session_id,
        launcher_template_id=str(app.get("template_id") or ""),
        launcher_display_name=str(app.get("display_name") or ""),
        preferred_provider=effective_provider,
        selected_agents=effective_agents,
        service_targets=_service_targets(effective_agents, preferred_provider=effective_provider),
        launcher_unit_kind=str(app.get("unit_kind") or app.get("kind") or "").strip().lower(),
        launcher_unit_class=str(app.get("unit_class") or "").strip().lower(),
        launcher_instance_policy=str(app.get("instance_policy") or "").strip().lower(),
        workspace_scope=workspace_scope,
        workspace_path=workspace_path,
        goal_completion_policy=goal_completion_policy,
    )
    updated_session = get_session_settings(runtime_root, username=normalized_username, session_id=session_id) or session
    return {
        "app": app,
        "unit": app,
        "session": updated_session,
        "launch_plan": {
            "preferred_provider": effective_provider,
            "selected_agents": effective_agents,
            "service_targets": _service_targets(effective_agents, preferred_provider=effective_provider),
            "initial_prompt": effective_initial_prompt,
            "workspace_scope": workspace_scope,
            "workspace_path": workspace_path,
            "auto_send_initial_prompt": bool(launcher.get("auto_send_initial_prompt", bool(effective_initial_prompt))),
        },
    }


def launch_unit(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return launch_session_template(*args, **kwargs)
