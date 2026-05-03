from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from plugin_catalog import list_plugin_session_template_descriptors
from runtime.persistent_state_pkg import (
    SESSION_GROUP_DEFAULT_PERMISSIONS,
    session_template_metadata_path,
    session_templates_dir,
    create_child_conversation_session,
    create_conversation_session,
    ensure_session_template_workspace,
    get_session_settings,
    list_sessions,
    normalize_username,
    read_json_file,
    state_lock,
    state_read_lock,
    update_session_goal,
    update_session_goal_flags,
    update_session_launcher_profile,
    update_session_selected_agents,
    write_json_file,
)
from wire.protocol import utc_ts

VALID_PROVIDERS = {"codex", "claude", "gemini"}
POOL_TOKENS = {"codex_pool": "codex", "claude_pool": "claude", "gemini_pool": "gemini"}
WORKSPACE_SCOPES = {"none", "app"}
SCHEDULE_KINDS = {"daily"}
SCHEDULE_RETRY_AFTER_SECONDS = 60


def _read_unit_metadata(runtime_root: Path, *, username: str, template_id: str) -> dict[str, Any] | None:
    metadata_path = session_template_metadata_path(runtime_root, username=username, template_id=template_id)
    metadata = read_json_file(metadata_path)
    if isinstance(metadata, dict):
        return metadata
    legacy_path = metadata_path.with_name("app.json")
    legacy = read_json_file(legacy_path)
    return legacy if isinstance(legacy, dict) else None


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


def _normalize_schedule(value: Any) -> dict[str, Any]:
    schedule = dict(value or {}) if isinstance(value, dict) else {}
    enabled = bool(schedule.get("enabled", bool(schedule)))
    kind = _normalize_schedule_kind(schedule.get("kind"))
    timezone = _normalize_schedule_timezone(schedule.get("timezone"))
    daily_time = _normalize_daily_time(schedule.get("daily_time") or schedule.get("time"))
    return {
        "enabled": enabled,
        "kind": kind,
        "timezone": timezone,
        "daily_time": daily_time,
        "summary": f"daily {daily_time} {timezone}",
    }


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


def list_registered_session_template_states(runtime_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with state_read_lock(runtime_root):
        app_root = session_templates_dir(runtime_root)
        if not app_root.exists():
            return records
        for user_dir in sorted(path for path in app_root.iterdir() if path.is_dir()):
            username = normalize_username(user_dir.name)
            for session_template_dir_entry in sorted(path for path in user_dir.iterdir() if path.is_dir()):
                metadata = _read_unit_metadata(runtime_root, username=username, template_id=session_template_dir_entry.name)
                if not isinstance(metadata, dict):
                    continue
                record = dict(metadata)
                record["username"] = username
                record["unit_id"] = str(record.get("unit_id") or record.get("template_id") or session_template_dir_entry.name).strip() or session_template_dir_entry.name
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
        metadata_path = session_template_metadata_path(runtime_root, username=normalized_username, template_id=normalized_template_id)
        current = read_json_file(metadata_path) or {}
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


def describe_app_schedule(
    app: dict[str, Any],
    *,
    app_state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    schedule = dict(((app.get("launcher") or {}).get("schedule")) or {})
    effective_now = (now or datetime.now(UTC)).astimezone(UTC)
    schedule_state = dict((app_state or {}).get("schedule_state") or {})
    descriptor = {
        **schedule,
        "due": False,
        "app_registered": bool(app_state),
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


def resolve_app_launch_parent_session_id(
    runtime_root: Path,
    *,
    username: str,
    app_state: dict[str, Any] | None = None,
) -> str | None:
    normalized_username = normalize_username(username)
    candidates: list[str] = []
    if isinstance(app_state, dict):
        previous_parent_session_id = str(app_state.get("last_parent_session_id") or "").strip()
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


def build_scheduled_app_session_label(app: dict[str, Any], schedule_info: dict[str, Any]) -> str:
    display_name = str(app.get("display_name") or app.get("unit_id") or app.get("template_id") or "Unit").strip() or "Unit"
    local_when = str(schedule_info.get("scheduled_for_local") or "").strip()
    timezone_name = str(schedule_info.get("timezone") or "UTC").strip() or "UTC"
    if not local_when:
        return f"{display_name} Scheduled Run"
    return f"{display_name} {local_when} {timezone_name}"


def build_scheduled_app_initial_prompt(app: dict[str, Any], schedule_info: dict[str, Any]) -> str:
    launcher = dict(app.get("launcher") or {})
    base_prompt = str(launcher.get("initial_prompt") or "").strip()
    lines = [
        "<aize_scheduled_unit_launch>",
        f"  <unit_id>{app.get('unit_id') or app.get('template_id') or ''}</unit_id>",
        f"  <scheduled_for_utc>{schedule_info.get('scheduled_for_utc') or ''}</scheduled_for_utc>",
        f"  <scheduled_for_local timezone=\"{schedule_info.get('timezone') or 'UTC'}\">{schedule_info.get('scheduled_for_local') or ''}</scheduled_for_local>",
        "  <instruction>This session was created automatically from the UnitFile's wall-clock schedule. Treat this as a fresh run, execute the unit goal now, and use the unit workspace for durable state instead of relying on prior session-local context.</instruction>",
        "</aize_scheduled_unit_launch>",
    ]
    schedule_prompt = "\n".join(lines).strip()
    return f"{schedule_prompt}\n\n{base_prompt}".strip() if base_prompt else schedule_prompt


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
            "session_permissions": session_permissions,
            "workspace_scope": _normalize_workspace_scope(launcher.get("workspace_scope")),
            "ui_url": str(launcher.get("ui_url") or interfaces.get("web") or "").strip(),
            "auto_select_session": bool(launcher.get("auto_select_session", True)),
            "auto_send_initial_prompt": bool(launcher.get("auto_send_initial_prompt", bool(initial_prompt))),
            "schedule": _normalize_schedule(launcher.get("schedule")),
        },
    }
    return normalized


def list_launchable_session_templates(*, default_provider: str) -> list[dict[str, Any]]:
    apps = [
        normalize_session_template_descriptor(descriptor, default_provider=default_provider)
        for descriptor in list_plugin_session_template_descriptors()
        if bool(descriptor.get("enabled", True))
    ]
    apps.sort(key=lambda item: (str(item.get("plugin_id") or ""), str(item.get("display_name") or "")))
    return apps


def list_launchable_units(*, default_provider: str) -> list[dict[str, Any]]:
    return list_launchable_session_templates(default_provider=default_provider)


def get_launchable_session_template(template_id: str, *, default_provider: str) -> dict[str, Any]:
    normalized_template_id = str(template_id or "").strip()
    for app in list_launchable_session_templates(default_provider=default_provider):
        if app["template_id"] == normalized_template_id:
            return app
    raise KeyError(normalized_template_id)


def get_launchable_unit(unit_id: str, *, default_provider: str) -> dict[str, Any]:
    return get_launchable_session_template(unit_id, default_provider=default_provider)


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
    mode = str(launcher.get("mode") or "create_child_session").strip().lower()
    session_group = str(launcher.get("session_group") or "user").strip().lower() or "user"
    session_ui_mode = str(launcher.get("session_ui_mode") or "standard").strip().lower() or "standard"
    session_interactive = bool(launcher.get("session_interactive", session_ui_mode == "communication"))
    communication_agent_enabled = bool(launcher.get("communication_agent_enabled", session_interactive))
    communication_agent_priority = _normalize_provider_profile_priority(launcher.get("communication_agent_priority"))
    session_permissions = dict(launcher.get("session_permissions") or {})
    workspace_scope = _normalize_workspace_scope(launcher.get("workspace_scope"))

    if mode == "create_session":
        session = create_conversation_session(
            runtime_root,
            username=normalized_username,
            label=effective_label,
            session_group=session_group,
            session_permissions=session_permissions,
            created_by_username=normalized_username,
            created_by_type="unit",
            origin_session_id=parent_session_id,
            session_ui_mode=session_ui_mode,
            session_interactive=session_interactive,
            communication_agent_enabled=communication_agent_enabled,
            communication_agent_priority=communication_agent_priority,
        )
        if effective_goal_text:
            session = update_session_goal(
                runtime_root,
                username=normalized_username,
                session_id=str(session["session_id"]),
                goal_text=effective_goal_text,
                updated_by_username=normalized_username,
                updated_by_type="unit",
                origin_session_id=parent_session_id,
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
            origin_session_id=parent_session_id,
            session_ui_mode=session_ui_mode,
            session_interactive=session_interactive,
            communication_agent_enabled=communication_agent_enabled,
            communication_agent_priority=communication_agent_priority,
        )
        if not session:
            raise RuntimeError("parent_session_not_found")

    session_id = str(session.get("session_id") or "").strip()
    if not session_id:
        raise RuntimeError("session_launch_failed")
    workspace_path = ""
    if workspace_scope == "app":
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
    update_session_launcher_profile(
        runtime_root,
        username=normalized_username,
        session_id=session_id,
        launcher_template_id=str(app.get("template_id") or ""),
        launcher_display_name=str(app.get("display_name") or ""),
        preferred_provider=effective_provider,
        selected_agents=effective_agents,
        service_targets=_service_targets(effective_agents, preferred_provider=effective_provider),
        workspace_scope=workspace_scope,
        workspace_path=workspace_path,
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
