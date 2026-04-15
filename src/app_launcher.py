from __future__ import annotations

from typing import Any

from plugin_catalog import list_plugin_app_descriptors
from runtime.persistent_state import (
    SESSION_GROUP_DEFAULT_PERMISSIONS,
    create_child_conversation_session,
    create_conversation_session,
    get_session_settings,
    normalize_username,
    update_session_goal,
    update_session_goal_flags,
    update_session_launcher_profile,
    update_session_selected_agents,
)

VALID_PROVIDERS = {"codex", "claude", "gemini"}
POOL_TOKENS = {"codex_pool": "codex", "claude_pool": "claude", "gemini_pool": "gemini"}


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


def normalize_app_descriptor(descriptor: dict[str, Any], *, default_provider: str) -> dict[str, Any]:
    app_id = str(descriptor.get("app_id") or "").strip()
    if not app_id:
        source = descriptor.get("_descriptor_path", "<unknown>")
        raise RuntimeError(f"app descriptor missing app_id: {source}")
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
    default_label = str(launcher.get("default_label") or descriptor.get("display_name") or app_id).strip() or app_id
    normalized = {
        "app_id": app_id,
        "plugin_id": str(descriptor.get("plugin_id") or "").strip(),
        "display_name": str(descriptor.get("display_name") or app_id).strip() or app_id,
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
            "session_permissions": session_permissions,
            "auto_select_session": bool(launcher.get("auto_select_session", True)),
            "auto_send_initial_prompt": bool(launcher.get("auto_send_initial_prompt", bool(initial_prompt))),
        },
    }
    return normalized


def list_launchable_apps(*, default_provider: str) -> list[dict[str, Any]]:
    apps = [
        normalize_app_descriptor(descriptor, default_provider=default_provider)
        for descriptor in list_plugin_app_descriptors()
        if bool(descriptor.get("enabled", True))
    ]
    apps.sort(key=lambda item: (str(item.get("plugin_id") or ""), str(item.get("display_name") or "")))
    return apps


def get_launchable_app(app_id: str, *, default_provider: str) -> dict[str, Any]:
    normalized_app_id = str(app_id or "").strip()
    for app in list_launchable_apps(default_provider=default_provider):
        if app["app_id"] == normalized_app_id:
            return app
    raise KeyError(normalized_app_id)


def launch_app_session(
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
    effective_label = str(label or launcher.get("default_label") or app.get("display_name") or app.get("app_id") or "").strip()
    effective_goal_text = str(goal_text if goal_text is not None else launcher.get("goal_text") or "").strip()
    effective_initial_prompt = str(initial_prompt if initial_prompt is not None else launcher.get("initial_prompt") or "").strip()
    mode = str(launcher.get("mode") or "create_child_session").strip().lower()
    session_group = str(launcher.get("session_group") or "user").strip().lower() or "user"
    session_permissions = dict(launcher.get("session_permissions") or {})

    if mode == "create_session":
        session = create_conversation_session(
            runtime_root,
            username=normalized_username,
            label=effective_label,
            session_group=session_group,
            session_permissions=session_permissions,
            created_by_username=normalized_username,
            created_by_type="app",
            origin_session_id=parent_session_id,
        )
        if effective_goal_text:
            session = update_session_goal(
                runtime_root,
                username=normalized_username,
                session_id=str(session["session_id"]),
                goal_text=effective_goal_text,
                updated_by_username=normalized_username,
                updated_by_type="app",
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
            created_by_type="app",
            origin_session_id=parent_session_id,
        )
        if not session:
            raise RuntimeError("parent_session_not_found")

    session_id = str(session.get("session_id") or "").strip()
    if not session_id:
        raise RuntimeError("session_launch_failed")
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
        launcher_app_id=str(app.get("app_id") or ""),
        launcher_display_name=str(app.get("display_name") or ""),
        preferred_provider=effective_provider,
        selected_agents=effective_agents,
        service_targets=_service_targets(effective_agents, preferred_provider=effective_provider),
    )
    updated_session = get_session_settings(runtime_root, username=normalized_username, session_id=session_id) or session
    return {
        "app": app,
        "session": updated_session,
        "launch_plan": {
            "preferred_provider": effective_provider,
            "selected_agents": effective_agents,
            "service_targets": _service_targets(effective_agents, preferred_provider=effective_provider),
            "initial_prompt": effective_initial_prompt,
            "auto_send_initial_prompt": bool(launcher.get("auto_send_initial_prompt", bool(effective_initial_prompt))),
        },
    }
