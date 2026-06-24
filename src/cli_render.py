from __future__ import annotations

import json
import re
from typing import Any

from store_defs import GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE, StoreError, payload_body, payload_files

DEFAULT_MESSAGE_LIMIT = 10


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def short_text(value: Any, *, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)]}..."


def tail_items(items: list[dict[str, Any]], limit: int | None = DEFAULT_MESSAGE_LIMIT) -> list[dict[str, Any]]:
    if limit is None or limit == 0:
        return items
    if limit < 0:
        raise StoreError("limit must be 0 or greater")
    return items[-limit:]


def console_body(value: Any, *, limit: int = 500) -> str:
    text = str(value or "")
    unescaped = (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .strip()
    )
    output_part = unescaped.split("\nstderr:", 1)[0].strip()
    tagged_values: list[str] = []
    for tag in ("body", "message"):
        tagged_values.extend(
            value.strip()
            for value in re.findall(fr"<{tag}>(.*?)</{tag}>", output_part, flags=re.DOTALL)
            if value.strip()
        )
    if tagged_values:
        text = tagged_values[-1]
        if "AIZE_GOAL_REASON:" in text:
            text = text.split("AIZE_GOAL_REASON:", 1)[1].splitlines()[0].strip()
        return short_text(text, limit=limit)
    for line in output_part.splitlines():
        line = line.strip()
        if not line or line.startswith("<") or line.startswith("AIZE_GOAL_STATUS:"):
            continue
        return short_text(line, limit=limit)
    tagged_values = []
    for tag in ("body", "message"):
        tagged_values.extend(
            value.strip()
            for value in re.findall(fr"<{tag}>(.*?)</{tag}>", unescaped, flags=re.DOTALL)
            if value.strip()
        )
    if tagged_values:
        return short_text(tagged_values[-1], limit=limit)
    return short_text(unescaped, limit=limit)


def print_kv(title: str, rows: list[tuple[str, Any]]) -> None:
    print(title)
    for key, value in rows:
        print(f"  {key}: {value}")


def print_units(units: list[dict[str, Any]]) -> None:
    if not units:
        print("No units.")
        return
    print(f"Units ({len(units)})")
    for unit in units:
        singleton = f" singleton={unit.get('singleton_session_id')}" if unit.get("singleton_session_id") else ""
        triggers = unit.get("activation_triggers") if isinstance(unit.get("activation_triggers"), dict) else {}
        trigger_text = ",".join(key for key in ("manual", "scheduled", "startup") if triggers.get(key)) or "none"
        print(
            f"- {unit.get('unit_id')} policy={unit.get('instance_policy')} "
            f"triggers={trigger_text} status={unit.get('status')}{singleton}"
        )


def current_goal_by_session(goals: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    current: dict[str, dict[str, Any]] = {}
    for goal in goals:
        session_id = str(goal.get("session_id") or "")
        if not session_id:
            continue
        current[session_id] = goal
    return current


def dispatch_state_by_goal(
    queue: list[dict[str, Any]] | None = None,
    runs: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    states: dict[str, str] = {}
    for run in runs or []:
        if run.get("lease_state") != "acquired":
            continue
        goal_id = str(run.get("goal_id") or "")
        if goal_id:
            states[goal_id] = "running"
    for entry in queue or []:
        if entry.get("status") != "queued":
            continue
        goal_id = str(entry.get("goal_id") or "")
        if goal_id and states.get(goal_id) != "running":
            states[goal_id] = "queued"
    return states


def agent_allocations_by_session(runs: list[dict[str, Any]] | None = None) -> dict[str, dict[str, int]]:
    allocations: dict[str, dict[str, int]] = {}
    for run in runs or []:
        if run.get("lease_state") != "acquired":
            continue
        session_id = str(run.get("session_id") or "")
        if not session_id:
            continue
        session_allocations = allocations.setdefault(session_id, {GOAL_MANAGER_ROLE: 0, WORKER_AGENT_ROLE: 0})
        phase = str(run.get("current_phase") or GOAL_MANAGER_ROLE)
        if phase == WORKER_AGENT_ROLE:
            session_allocations[WORKER_AGENT_ROLE] += 1
        else:
            session_allocations[GOAL_MANAGER_ROLE] += 1
    return allocations


def agent_pool_snapshot(
    profiles: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    active_runs = [run for run in runs if run.get("lease_state") == "acquired"]
    allocated = {
        GOAL_MANAGER_ROLE: sum(1 for run in active_runs if str(run.get("current_phase") or GOAL_MANAGER_ROLE) != WORKER_AGENT_ROLE),
        WORKER_AGENT_ROLE: sum(1 for run in active_runs if str(run.get("current_phase") or "") == WORKER_AGENT_ROLE),
    }
    profiles_by_role = {str(profile.get("role") or ""): profile for profile in profiles}
    roles = []
    for role in (GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE):
        profile = profiles_by_role.get(role, {})
        roles.append(
            {
                "role": role,
                "provider": profile.get("provider") or "",
                "status": profile.get("status") or "",
                "allocated": allocated[role],
            }
        )
    return {
        "roles": roles,
        "total_allocated": dict(allocated),
        "active_runs": [
            {
                "run_id": run.get("run_id"),
                "session_id": run.get("session_id"),
                "goal_id": run.get("goal_id"),
                "current_phase": run.get("current_phase"),
                "lease_acquired_at": run.get("lease_acquired_at"),
            }
            for run in active_runs
        ],
    }


def print_sessions(
    sessions: list[dict[str, Any]],
    *,
    goals: list[dict[str, Any]] | None = None,
    queue: list[dict[str, Any]] | None = None,
    runs: list[dict[str, Any]] | None = None,
) -> None:
    if not sessions:
        print("No sessions.")
        return
    current_goals = current_goal_by_session(goals or [])
    dispatch_states = dispatch_state_by_goal(queue, runs)
    agent_allocations = agent_allocations_by_session(runs)
    print(f"Sessions ({len(sessions)})")
    for session in sessions:
        active = "Active" if session.get("active") is True else "Inactive"
        singleton = " singleton" if session.get("singleton") else ""
        unit = session.get("unit_id") or "none"
        session_id = str(session.get("session_id") or "")
        goal = current_goals.get(session_id)
        goal_state = str(goal.get("completion_state") or "incomplete") if goal else "none"
        if goals is None:
            state_text = active
        else:
            display_goal_state = "NoGoal" if goal_state == "none" else goal_state.capitalize()
            state_text = f"{active}, {display_goal_state}"
        dispatch_note = ""
        allocation = agent_allocations.get(session_id, {GOAL_MANAGER_ROLE: 0, WORKER_AGENT_ROLE: 0})
        allocation_text = f" G:{allocation.get(GOAL_MANAGER_ROLE, 0)},W:{allocation.get(WORKER_AGENT_ROLE, 0)}" if goals is not None else ""
        if goal:
            dispatch_state = dispatch_states.get(str(goal.get("goal_id") or ""))
            if dispatch_state:
                dispatch_note = f" {dispatch_state}"
            elif session.get("active") is True and goal_state == "incomplete":
                dispatch_note = " dispatchable"
        print(f"- {session_id} [{state_text}]{allocation_text} unit={unit}{singleton}{dispatch_note}")


def print_session(session: dict[str, Any]) -> None:
    active = "Active" if session.get("active") is True else "Inactive"
    print_kv(
        f"Session {session.get('session_id')}",
        [
            ("state", active),
            ("unit", session.get("unit_id") or "none"),
            ("singleton", bool(session.get("singleton"))),
            ("created", session.get("created_at")),
            ("updated", session.get("updated_at")),
        ],
    )


def print_goals(goals: list[dict[str, Any]]) -> None:
    if not goals:
        print("No goals.")
        return
    print(f"Goals ({len(goals)})")
    for goal in goals:
        body = short_text(goal.get("body"), limit=120)
        print(
            f"- {goal.get('goal_id')} [{goal.get('completion_state')}] "
            f"session={goal.get('session_id')}"
        )
        if body:
            print(f"  goal: {body}")


def print_messages(messages: list[dict[str, Any]]) -> None:
    if not messages:
        print("No messages.")
        return
    print(f"Messages ({len(messages)})")
    for message in messages:
        files = payload_files(message)
        payload_note = f" files={len(files)}" if files else ""
        print(
            f"- {message.get('message_id')}: "
            f"{message.get('from')} -> {message.get('to')}{payload_note}"
        )
        body = short_text(payload_body(message), limit=160)
        if body:
            print(f"  {body}")


def print_agent_threads(threads: list[dict[str, Any]]) -> None:
    if not threads:
        print("No agent threads.")
        return
    print(f"Agent threads ({len(threads)})")
    for thread in threads:
        print(
            f"- {thread.get('role')} session={thread.get('session_id')} "
            f"turns={len(thread.get('turns') or [])} resume={thread.get('resume_token')}"
        )


def print_agent_pool(snapshot: dict[str, Any]) -> None:
    roles = snapshot.get("roles") if isinstance(snapshot.get("roles"), list) else []
    active_runs = snapshot.get("active_runs") if isinstance(snapshot.get("active_runs"), list) else []
    print("Agent pool")
    if not roles:
        print("- no agent roles")
    for role in roles:
        print(
            f"- {role.get('role')} provider={role.get('provider') or 'none'} "
            f"status={role.get('status') or 'unknown'} allocated={role.get('allocated') or 0}"
        )
    if not active_runs:
        print("Active runs: none")
        return
    print(f"Active runs ({len(active_runs)})")
    for run in active_runs:
        print(
            f"- {run.get('run_id')} session={run.get('session_id')} "
            f"goal={run.get('goal_id')} phase={run.get('current_phase') or 'unknown'} "
            f"acquired={run.get('lease_acquired_at')}"
        )


def print_dispatch_runs(runs: list[dict[str, Any]]) -> None:
    if not runs:
        print("No dispatch runs.")
        return
    print(f"Dispatch runs ({len(runs)})")
    for run in runs:
        print(
            f"- {run.get('run_id')} session={run.get('session_id')} goal={run.get('goal_id')} "
            f"completion={run.get('completion_state', 'unknown')} lease={run.get('lease_state')}"
        )
        phases = [str(step.get("phase")) for step in run.get("steps", []) if isinstance(step, dict)]
        if phases:
            print(f"  steps: {', '.join(phases)}")


def print_dispatch_requests(entries: list[dict[str, Any]]) -> None:
    if not entries:
        print("Dispatch requests: empty")
        return
    print(f"Dispatch requests ({len(entries)})")
    for entry in entries:
        print(
            f"- {entry.get('request_id')} [{entry.get('status')}] "
            f"session={entry.get('session_id')} goal={entry.get('goal_id')} "
            f"role={entry.get('role') or GOAL_MANAGER_ROLE} priority={entry.get('priority')}"
        )
        reason = short_text(entry.get("reason"), limit=120)
        if reason:
            print(f"  reason: {reason}")
        if entry.get("available_after"):
            print(f"  available_after: {entry.get('available_after')}")


def print_graph(
    graph: dict[str, Any],
    *,
    goals: list[dict[str, Any]] | None = None,
    queue: list[dict[str, Any]] | None = None,
    runs: list[dict[str, Any]] | None = None,
) -> None:
    print_sessions(list(graph.get("sessions") or []), goals=goals, queue=queue, runs=runs)
    edges = list(graph.get("edges") or [])
    if not edges:
        print("Edges: none")
        return
    print(f"Edges ({len(edges)})")
    for edge in edges:
        print(f"- {edge.get('parent_session_id')} -> {edge.get('child_session_id')}")


def print_dispatch_result(result: dict[str, Any] | None) -> None:
    if result is None:
        print("No dispatchable Active + Incomplete goal.")
        return
    goal = result.get("goal") if isinstance(result.get("goal"), dict) else {}
    run = result.get("run") if isinstance(result.get("run"), dict) else {}
    print("Dispatched")
    print(f"  session: {run.get('session_id')}")
    goal_text = short_text(goal.get("body"), limit=120)
    print(f"  goal: {goal_text} [{goal.get('completion_state')}]")
    print(f"  run: {run.get('run_id')} lease={run.get('lease_state')}")
    phases = [str(step.get("phase")) for step in run.get("steps", []) if isinstance(step, dict)]
    if phases:
        print(f"  steps: {', '.join(phases)}")


def print_created_goal_session(payload: dict[str, Any]) -> None:
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else {}
    print("Created goal session")
    print(f"  session: {session.get('session_id')} unit={session.get('unit_id') or 'none'}")
    title = short_text(session.get("title"), limit=80)
    if title:
        print(f"  title: {title}")
    goal_text = short_text(goal.get("body"), limit=120)
    print(f"  goal: {goal_text} [{goal.get('completion_state')}]")


def print_created_session(session: dict[str, Any]) -> None:
    print("Created session")
    print(f"  session: {session.get('session_id')} unit={session.get('unit_id') or 'none'}")
    print(f"  state: {'Active' if session.get('active') is True else 'Inactive'}")
