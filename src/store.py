from __future__ import annotations

import json
import os
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from model import Session, Unit, new_id, utc_now


from store_defs import (
    DEFAULT_AGENT_PROVIDER,
    DEFAULT_ROOT_PASSWORD,
    GOAL_MANAGER_ROLE,
    ROOT_SESSION_ID,
    ROOT_UNIT_ID,
    ROOT_USERNAME,
    STATE_VERSION,
    WORKER_AGENT_ROLE,
    StoreError,
    account_home_unit_id,
    account_home_session_id,
    session_endpoint,
)
from store_auth import AuthMixin
from store_dispatch import DispatchMixin
from store_dispatch_requests import DispatchRequestMixin
from store_message import MessageMixin
from store_prompts import PromptMixin
from store_query import QueryMixin
from store_session import SessionGoalMixin
from store_session_log import SessionLogMixin


class Store(
    AuthMixin,
    SessionLogMixin,
    SessionGoalMixin,
    MessageMixin,
    DispatchRequestMixin,
    PromptMixin,
    DispatchMixin,
    QueryMixin,
):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_path = root / "state.json"

    @contextmanager
    def _state_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / "state.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def init(self) -> dict[str, Any]:
        with self._state_lock():
            if self.state_path.exists():
                state = self.load()
                if self.ensure_defaults(state):
                    self.save(state)
                return state
            now = utc_now()
            state = {
                "version": STATE_VERSION,
                "created_at": now,
                "units": {},
                "sessions": {},
                "session_edges": [],
                "accounts": {},
                "goals": {},
                "agent_profiles": {},
                "agent_threads": {},
                "dispatch_runs": {},
                "dispatch_requests": [],
                "runtime_settings": {},
                "messages": [],
                "endpoint_cursors": {},
                "session_logs": {},
            }
            self.ensure_defaults(state, now=now)
            self.save(state)
            return state

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            raise StoreError(f"state not initialized: {self.state_path}")
        with self.state_path.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            raise StoreError("state root must be an object")
        if state.get("version") != STATE_VERSION:
            raise StoreError(f"unsupported state version: {state.get('version')}")
        state.setdefault("units", {})
        state.setdefault("sessions", {})
        state.setdefault("session_edges", [])
        state.setdefault("accounts", {})
        state.setdefault("goals", {})
        state.setdefault("agent_profiles", {})
        state.setdefault("agent_threads", {})
        state.setdefault("dispatch_runs", {})
        self._ensure_dispatch_requests_state(state)
        state.setdefault("runtime_settings", {})
        state.setdefault("messages", [])
        state.setdefault("endpoint_cursors", {})
        state.setdefault("session_logs", {})
        self.ensure_defaults(state)
        return state

    def ensure_defaults(self, state: dict[str, Any], *, now: str | None = None) -> bool:
        timestamp = now or utc_now()
        changed = False
        units = state.setdefault("units", {})
        sessions = state.setdefault("sessions", {})
        state.setdefault("session_edges", [])
        accounts = state.setdefault("accounts", {})
        state.setdefault("goals", {})
        agent_profiles = state.setdefault("agent_profiles", {})
        agent_threads = state.setdefault("agent_threads", {})
        state.setdefault("dispatch_runs", {})
        if self._ensure_dispatch_requests_state(state):
            changed = True
        runtime_settings = state.setdefault("runtime_settings", {})
        state.setdefault("messages", [])
        state.setdefault("endpoint_cursors", {})
        state.setdefault("session_logs", {})
        if "dispatch_lot_size" not in runtime_settings:
            runtime_settings["dispatch_lot_size"] = 1
            changed = True
        if ROOT_UNIT_ID not in units:
            units[ROOT_UNIT_ID] = Unit(
                unit_id=ROOT_UNIT_ID,
                created_at=timestamp,
                instance_policy="singleton",
                singleton_session_id=ROOT_SESSION_ID,
            ).to_dict()
            changed = True
        else:
            root_unit = units[ROOT_UNIT_ID]
            if root_unit.get("instance_policy") != "singleton":
                root_unit["instance_policy"] = "singleton"
                changed = True
            if root_unit.get("singleton_session_id") != ROOT_SESSION_ID:
                root_unit["singleton_session_id"] = ROOT_SESSION_ID
                changed = True
        for unit in units.values():
            for key, default in {
                "display_name": "",
                "description": "",
                "goal_text": "",
                "initial_prompt": "",
                "schedule": {},
            }.items():
                if key not in unit:
                    unit[key] = dict(default) if isinstance(default, dict) else default
                    changed = True
            schedule = unit.get("schedule")
            if isinstance(schedule, dict) and schedule:
                normalized_schedule = self._normalize_unit_schedule(schedule)
                if schedule != normalized_schedule:
                    unit["schedule"] = normalized_schedule
                    changed = True
            triggers = unit.get("activation_triggers")
            normalized_triggers = self._normalize_unit_activation_triggers(
                triggers if isinstance(triggers, dict) and triggers else None,
                schedule=unit.get("schedule"),
            )
            if triggers != normalized_triggers:
                unit["activation_triggers"] = normalized_triggers
                changed = True
            if "owner_account" not in unit:
                unit["owner_account"] = "" if unit.get("unit_id") == ROOT_UNIT_ID else ROOT_USERNAME
                changed = True

        if ROOT_SESSION_ID not in sessions:
            sessions[ROOT_SESSION_ID] = Session(
                session_id=ROOT_SESSION_ID,
                unit_id=ROOT_UNIT_ID,
                created_at=timestamp,
                updated_at=timestamp,
                title=ROOT_SESSION_ID,
                singleton=True,
            ).to_dict()
            changed = True
        else:
            root_session = sessions[ROOT_SESSION_ID]
            if root_session.get("unit_id") != ROOT_UNIT_ID:
                raise StoreError("root session exists but is not owned by root unit")
            if root_session.get("singleton") is not True:
                root_session["singleton"] = True
                changed = True
        if ROOT_USERNAME not in accounts:
            accounts[ROOT_USERNAME] = self._build_account(
                ROOT_USERNAME,
                DEFAULT_ROOT_PASSWORD,
                roles=["root", "admin"],
                created_at=timestamp,
            ).to_dict()
            changed = True
        if self._ensure_account_home_sessions(state, now=timestamp):
            changed = True
        if self._ensure_unit_session_owner_parents(state):
            changed = True
        for session_id in list(sessions):
            if session_id == ROOT_SESSION_ID:
                continue
            if not self._has_path(state["session_edges"], ROOT_SESSION_ID, session_id):
                state["session_edges"].append(
                    {
                        "parent_session_id": ROOT_SESSION_ID,
                        "child_session_id": session_id,
                        "created_at": timestamp,
                    }
                )
                changed = True
        for role in (GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE):
            if role not in agent_profiles:
                agent_profiles[role] = {
                    "role": role,
                    "provider": DEFAULT_AGENT_PROVIDER,
                    "status": "active",
                    "created_at": timestamp,
                    "default_assignment": True,
                }
                changed = True
                continue
        if self._ensure_session_metadata(state):
            changed = True
        if self._ensure_session_log_defaults(state):
            changed = True
        return changed

    def _ensure_unit_session_owner_parents(self, state: dict[str, Any]) -> bool:
        changed = False
        units = state.setdefault("units", {})
        sessions = state.setdefault("sessions", {})
        accounts = state.setdefault("accounts", {})
        edges = state.setdefault("session_edges", [])
        for session_id, session in sessions.items():
            unit_id = str(session.get("unit_id") or "").strip()
            unit = units.get(unit_id)
            owner_account = str(unit.get("owner_account") or "").strip() if unit else ""
            account = accounts.get(owner_account)
            if not account:
                continue
            home_session_id = str(account.get("home_session_id") or "").strip()
            if not home_session_id or home_session_id == session_id:
                continue
            if home_session_id not in sessions:
                raise StoreError(f"owner account has no home session: {owner_account}")
            if not any(
                edge.get("parent_session_id") == home_session_id
                and edge.get("child_session_id") == session_id
                for edge in edges
            ):
                self._link_sessions_in_state(state, home_session_id, session_id)
                changed = True
            retained_edges = [
                edge
                for edge in edges
                if not (
                    edge.get("parent_session_id") == ROOT_SESSION_ID
                    and edge.get("child_session_id") == session_id
                )
            ]
            if len(retained_edges) != len(edges):
                edges[:] = retained_edges
                changed = True
        return changed

    def _ensure_account_home_sessions(self, state: dict[str, Any], *, now: str | None = None) -> bool:
        timestamp = now or utc_now()
        changed = False
        units = state.setdefault("units", {})
        sessions = state.setdefault("sessions", {})
        accounts = state.setdefault("accounts", {})
        state.setdefault("session_edges", [])
        for username, account in sorted(accounts.items()):
            home_session_id = account_home_session_id(username)
            previous_home_session_id = str(account.get("home_session_id") or "").strip()
            if previous_home_session_id and previous_home_session_id != home_session_id:
                if self._rename_session_id(state, previous_home_session_id, home_session_id):
                    changed = True
            if previous_home_session_id != home_session_id:
                account["home_session_id"] = home_session_id
                changed = True
            home_unit_id = account_home_unit_id(username)
            previous_home_unit_id = str(account.get("home_unit_id") or "").strip()
            if previous_home_unit_id and previous_home_unit_id != home_unit_id:
                previous_home_unit = units.pop(previous_home_unit_id, None)
                if previous_home_unit is not None:
                    if home_unit_id in units:
                        raise StoreError(f"account home unit already exists: {home_unit_id}")
                    previous_home_unit["unit_id"] = home_unit_id
                    units[home_unit_id] = previous_home_unit
                    changed = True
            if previous_home_unit_id != home_unit_id:
                account["home_unit_id"] = home_unit_id
                changed = True
            if home_session_id == ROOT_SESSION_ID:
                raise StoreError("account home session cannot be the system root session")
            home_unit = units.get(home_unit_id)
            if home_unit is None:
                home_unit = Unit(
                    unit_id=home_unit_id,
                    created_at=timestamp,
                    instance_policy="singleton",
                    singleton_session_id=home_session_id,
                    display_name=f"{username} account root",
                    description="Account root singleton Unit.",
                    activation_triggers={"manual": True, "scheduled": False, "startup": False},
                    workspace_path=self._unit_workspace_relpath(home_unit_id),
                    owner_account=username,
                ).to_dict()
                units[home_unit_id] = home_unit
                changed = True
            else:
                if home_unit.get("instance_policy") != "singleton":
                    home_unit["instance_policy"] = "singleton"
                    changed = True
                if home_unit.get("singleton_session_id") != home_session_id:
                    home_unit["singleton_session_id"] = home_session_id
                    changed = True
                if home_unit.get("owner_account") != username:
                    home_unit["owner_account"] = username
                    changed = True
            self._ensure_unit_workspace(home_unit)
            if home_session_id not in sessions:
                sessions[home_session_id] = Session(
                    session_id=home_session_id,
                    unit_id=home_unit_id,
                    created_at=timestamp,
                    updated_at=timestamp,
                    title=f"{username} home",
                    active=True,
                    singleton=True,
                ).to_dict()
                changed = True
            else:
                session = sessions[home_session_id]
                if session.get("unit_id") != home_unit_id:
                    session["unit_id"] = home_unit_id
                    changed = True
                if session.get("singleton") is not True:
                    session["singleton"] = True
                    changed = True
                if not str(session.get("title") or "").strip():
                    session["title"] = f"{username} home"
                    changed = True
            self._ensure_role_cursors(sessions[home_session_id])
            self._ensure_session_workspace(sessions[home_session_id])
            if not self._has_path(state["session_edges"], ROOT_SESSION_ID, home_session_id):
                self._link_sessions_in_state(state, ROOT_SESSION_ID, home_session_id)
                changed = True
        return changed

    def _rename_session_id(self, state: dict[str, Any], previous_id: str, session_id: str) -> bool:
        sessions = state.setdefault("sessions", {})
        if previous_id == session_id or previous_id not in sessions:
            return False
        if session_id in sessions:
            raise StoreError(f"account home session already exists: {session_id}")

        session = sessions.pop(previous_id)
        session["session_id"] = session_id
        sessions[session_id] = session

        for edge in state.setdefault("session_edges", []):
            if edge.get("parent_session_id") == previous_id:
                edge["parent_session_id"] = session_id
            if edge.get("child_session_id") == previous_id:
                edge["child_session_id"] = session_id
        for goal in state.setdefault("goals", {}).values():
            if goal.get("session_id") == previous_id:
                goal["session_id"] = session_id
        for request in state.setdefault("dispatch_requests", []):
            if request.get("session_id") == previous_id:
                request["session_id"] = session_id
        for run in state.setdefault("dispatch_runs", {}).values():
            if run.get("session_id") == previous_id:
                run["session_id"] = session_id

        previous_endpoint = session_endpoint(previous_id)
        endpoint = session_endpoint(session_id)
        for message in state.setdefault("messages", []):
            if message.get("from") == previous_endpoint:
                message["from"] = endpoint
            if message.get("to") == previous_endpoint:
                message["to"] = endpoint
        endpoint_cursors = state.setdefault("endpoint_cursors", {})
        if previous_endpoint in endpoint_cursors:
            endpoint_cursors[endpoint] = endpoint_cursors.pop(previous_endpoint)

        session_logs = state.setdefault("session_logs", {})
        if previous_id in session_logs:
            session_logs[session_id] = session_logs.pop(previous_id)

        agent_threads = state.setdefault("agent_threads", {})
        for thread_key, thread in list(agent_threads.items()):
            if thread.get("session_id") != previous_id:
                continue
            role = str(thread.get("role") or "")
            new_thread_key = f"{session_id}:{role}"
            thread["session_id"] = session_id
            thread["thread_id"] = new_thread_key
            agent_threads.pop(thread_key)
            agent_threads[new_thread_key] = thread

        for unit in state.setdefault("units", {}).values():
            if unit.get("singleton_session_id") == previous_id:
                unit["singleton_session_id"] = session_id
            schedule = unit.get("schedule")
            if not isinstance(schedule, dict):
                continue
            for key in ("next_run_required_by_session_id", "next_run_set_by_session_id"):
                if schedule.get(key) == previous_id:
                    schedule[key] = session_id
        return True

    def save(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.{new_id('tmp')}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
        tmp.replace(self.state_path)
