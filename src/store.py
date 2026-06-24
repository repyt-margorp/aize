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
        self.root.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            state = self.load()
            changed = self.ensure_defaults(state)
            if changed:
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
            "message_index": [],
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
        state.setdefault("message_index", [])
        state.setdefault("session_logs", {})
        changed = self.ensure_defaults(state)
        if changed:
            self.save(state)
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
        state.setdefault("message_index", [])
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
        if ROOT_USERNAME not in accounts:
            accounts[ROOT_USERNAME] = self._build_account(
                ROOT_USERNAME,
                DEFAULT_ROOT_PASSWORD,
                roles=["root", "admin"],
                created_at=timestamp,
            ).to_dict()
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

    def save(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.{new_id('tmp')}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
        tmp.replace(self.state_path)
