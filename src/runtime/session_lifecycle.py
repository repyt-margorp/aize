from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from runtime.communication_goal import is_continuous_communication_session
from runtime.dispatch_queue import dispatch_priority
from runtime.message_builder import make_dispatch_pending_message
from runtime.persistent_state_pkg import (
    active_goal_manager_priority,
    append_goal_manager_pending_input,
    drain_goal_manager_pending_inputs,
    get_session_settings,
    get_session_service,
    join_session_agent,
    lease_session_service,
    read_json_file,
    resolve_session_agent_id,
    session_goal_manager_state_path,
    write_json_file,
)
from runtime.session_view import session_has_active_in_progress_goal
from wire.protocol import utc_ts


def _dedupe_work_items(items: list[Any], new_item: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen = False
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized_item = dict(item)
        normalized.append(normalized_item)
        if (
            str(normalized_item.get("kind") or "") == str(new_item.get("kind") or "")
            and str(normalized_item.get("reason") or "") == str(new_item.get("reason") or "")
            and str(normalized_item.get("source_service_id") or "")
            == str(new_item.get("source_service_id") or "")
        ):
            seen = True
    if not seen:
        normalized.append(dict(new_item))
    return normalized


def _provider_from_service_id(service_id: str) -> str:
    normalized = str(service_id or "").strip().lower()
    for provider in ("codex", "claude", "gemini"):
        if normalized.startswith(f"service-{provider}-"):
            return provider
    return ""


def _resolve_goal_manager_service(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    session: dict[str, Any],
    goal_manager_state: dict[str, Any],
    service_pools_by_provider: dict[str, list[str]],
    default_provider: str,
) -> str:
    existing_state = str(goal_manager_state.get("state") or "").strip().lower()
    existing_gm_service_id = str(goal_manager_state.get("service_id") or "").strip()
    all_pool_services = {
        service_id
        for service_ids in service_pools_by_provider.values()
        for service_id in service_ids
    }
    if existing_state in {"running", "queued"} and existing_gm_service_id in all_pool_services:
        return existing_gm_service_id

    bound_service_id = str(get_session_service(runtime_root, username=username, session_id=session_id) or "").strip()
    bound_provider = _provider_from_service_id(bound_service_id)
    if bound_service_id and bound_provider and bound_service_id in service_pools_by_provider.get(bound_provider, []):
        join_session_agent(
            runtime_root,
            username=username,
            session_id=session_id,
            service_id=bound_service_id,
            provider=bound_provider,
            role="goal_manager",
            transport="lifecycle_review",
        )
        return bound_service_id

    priority = active_goal_manager_priority(session.get("goal_manager_priority"), available_kinds=None)
    if not priority:
        preferred = str(session.get("preferred_provider") or default_provider or "codex").strip().lower() or "codex"
        priority = [preferred]
    for provider in priority:
        pool = service_pools_by_provider.get(str(provider or "").strip().lower(), [])
        if not pool:
            continue
        leased = lease_session_service(
            runtime_root,
            username=username,
            session_id=session_id,
            pool_service_ids=pool,
        )
        if not leased:
            continue
        join_session_agent(
            runtime_root,
            username=username,
            session_id=session_id,
            service_id=leased,
            provider=str(provider),
            role="goal_manager",
            transport="lifecycle_review",
        )
        return str(leased)
    return ""


def _is_released_nonrunnable_reason(reason: str) -> bool:
    return str(reason or "").strip().startswith("released_nonrunnable_session_service:")


def purge_continuous_communication_restart_owner_lost_state(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    state_path: Path | None = None,
    goal_manager_state: dict[str, Any] | None = None,
) -> bool:
    """Clear any stale lifecycle_owner_lost entries left from the pre-guard restart loop.

    Continuous communication sessions intentionally run an ephemeral interactive
    worker. Restart bookkeeping that releases that worker should not leave a
    perpetual GoalManager review item queued, since the worker will respawn on
    the next user input and there is no real ownership transfer to verify.

    Returns True if any stale entry was removed.
    """

    if state_path is None:
        state_path = session_goal_manager_state_path(
            runtime_root,
            username=username,
            session_id=session_id,
        )
    if goal_manager_state is None:
        loaded_state = read_json_file(state_path) or {}
        goal_manager_state = loaded_state if isinstance(loaded_state, dict) else {}

    def is_restart_bookkeeping_lifecycle_item(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        if str(item.get("kind") or "") != "lifecycle_owner_lost":
            return False
        return _is_released_nonrunnable_reason(str(item.get("reason") or ""))

    purged = False
    existing_items = goal_manager_state.get("pending_work_items")
    filtered_items = [
        item
        for item in (existing_items if isinstance(existing_items, list) else [])
        if not is_restart_bookkeeping_lifecycle_item(item)
    ]
    if isinstance(existing_items, list) and len(filtered_items) != len(existing_items):
        goal_manager_state["pending_work_items"] = filtered_items
        goal_manager_state["updated_at"] = utc_ts()
        write_json_file(state_path, goal_manager_state)
        purged = True

    drained = drain_goal_manager_pending_inputs(
        runtime_root,
        username=username,
        session_id=session_id,
    )
    surviving = [item for item in drained if not is_restart_bookkeeping_lifecycle_item(item)]
    if len(surviving) != len(drained):
        purged = True
    for item in surviving:
        append_goal_manager_pending_input(
            runtime_root,
            username=username,
            session_id=session_id,
            entry=item,
        )
    return purged


def enqueue_goal_manager_lifecycle_review(
    runtime_root: Path,
    *,
    manifest: dict[str, Any],
    from_service_id: str,
    process_id: str,
    username: str,
    session_id: str,
    reason: str,
    source_service_id: str,
    service_pools_by_provider: dict[str, list[str]],
    default_provider: str = "codex",
    send_dispatch: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    session = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
    if not session_has_active_in_progress_goal(session):
        return {"queued": False, "error": "goal_state_disallows_dispatch"}

    state_path = session_goal_manager_state_path(runtime_root, username=username, session_id=session_id)
    goal_manager_state = read_json_file(state_path) or {}
    if not isinstance(goal_manager_state, dict):
        goal_manager_state = {}

    if is_continuous_communication_session(session) and _is_released_nonrunnable_reason(reason):
        purge_continuous_communication_restart_owner_lost_state(
            runtime_root,
            username=username,
            session_id=session_id,
            state_path=state_path,
            goal_manager_state=goal_manager_state,
        )
        return {
            "queued": False,
            "error": "skipped_continuous_communication_ephemeral_owner_restart",
        }
    target_service_id = _resolve_goal_manager_service(
        runtime_root,
        username=username,
        session_id=session_id,
        session=session,
        goal_manager_state=goal_manager_state,
        service_pools_by_provider=service_pools_by_provider,
        default_provider=default_provider,
    )
    if not target_service_id:
        return {"queued": False, "error": "no_available_goal_manager_worker"}

    work_item = {
        "kind": "lifecycle_owner_lost",
        "ts": utc_ts(),
        "service_id": target_service_id,
        "source_service_id": str(source_service_id or "").strip(),
        "goal_id": str(session.get("active_goal_id") or session.get("goal_id") or "").strip(),
        "reason": str(reason or "non_goal_manager_owner_unavailable").strip(),
    }
    append_goal_manager_pending_input(
        runtime_root,
        username=username,
        session_id=session_id,
        entry=work_item,
    )
    goal_manager_state.update(
        {
            "state": "queued",
            "service_id": target_service_id,
            "pending_work_items": _dedupe_work_items(
                list(goal_manager_state.get("pending_work_items", []))
                if isinstance(goal_manager_state.get("pending_work_items"), list)
                else [],
                work_item,
            ),
            "updated_at": utc_ts(),
        }
    )
    write_json_file(state_path, goal_manager_state)

    dispatch_message = make_dispatch_pending_message(
        manifest=manifest,
        from_service_id=from_service_id,
        to_service_id=target_service_id,
        process_id=process_id,
        run_id=f"goal-manager-lifecycle-{work_item['ts'].replace(':', '').replace('-', '')}",
        username=username,
        session_id=session_id,
        auth_context=None,
        reason="goal_manager_review",
        session_agent_id=resolve_session_agent_id(
            runtime_root,
            username=username,
            session_id=session_id,
            service_id=target_service_id,
            role="goal_manager",
        ),
        agent_profile={"session_slot": "goal_manager"},
        dispatch_priority=dispatch_priority("goal_manager_review"),
    )
    dispatch_sent = bool(send_dispatch(dispatch_message))
    return {
        "queued": True,
        "target_service_id": target_service_id,
        "dispatch_sent": dispatch_sent,
        "reason": work_item["reason"],
    }
