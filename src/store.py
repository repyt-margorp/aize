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
    DISPATCH_PRIORITY_GOAL,
    DISPATCH_PRIORITY_USER_INPUT,
    GOAL_MANAGER_ROLE,
    ROOT_SESSION_ID,
    ROOT_UNIT_ID,
    ROOT_USERNAME,
    STATE_VERSION,
    WORKER_AGENT_ROLE,
    StoreError,
    session_endpoint,
)
from store_auth import AuthMixin
from store_dispatch import DispatchMixin
from store_dispatch_queue import DispatchQueueMixin
from store_message import MessageMixin
from store_prompts import PromptMixin
from store_query import QueryMixin
from store_session import SessionGoalMixin


class Store(
    AuthMixin,
    SessionGoalMixin,
    MessageMixin,
    DispatchQueueMixin,
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
            "dispatch_queue": [],
            "runtime_settings": {},
            "messages": [],
            "endpoint_cursors": {},
            "message_index": [],
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
        state.setdefault("dispatch_queue", [])
        state.setdefault("runtime_settings", {})
        state.setdefault("messages", [])
        state.setdefault("endpoint_cursors", {})
        state.setdefault("message_index", [])
        changed = self.ensure_defaults(state)
        changed = self._reconcile_queued_user_inputs(state) or changed
        changed = self._reconcile_incomplete_goal_queue(state) or changed
        if changed:
            self.save(state)
        return state

    def _reconcile_queued_user_inputs(self, state: dict[str, Any]) -> bool:
        changed = False
        for session_id in state.get("sessions", {}):
            for message in self._messages_after_cursor(state, session_endpoint(session_id)):
                payload = message.get("payload")
                if not isinstance(payload, dict) or not payload.get("user_input"):
                    continue
                reason = f"UserInput message {message.get('message_id')} requires Session processing."
                if (
                    payload.get("defer_goal_manager_until_worker_report") is True
                    or payload.get("worker_request") is True
                ):
                    continue
                if payload.get("reprocess_goal_id"):
                    goal = state.get("goals", {}).get(str(payload.get("reprocess_goal_id") or ""))
                    if goal:
                        before_queue = json.dumps(state.setdefault("dispatch_queue", []), sort_keys=True)
                        self._enqueue_dispatch(
                            state,
                            goal,
                            priority=DISPATCH_PRIORITY_USER_INPUT,
                            reason=reason,
                            trigger_message_id=str(message.get("message_id") or ""),
                        )
                        after_queue = json.dumps(state.setdefault("dispatch_queue", []), sort_keys=True)
                        if after_queue != before_queue:
                            changed = True
                    continue
                actor = str(message.get("from") or "system")
                goal = self._mark_session_reprocess_needed(
                    state,
                    session_id=session_id,
                    actor=actor,
                    reason=reason,
                    priority=DISPATCH_PRIORITY_USER_INPUT,
                    created_at=str(message.get("created_at") or utc_now()),
                    trigger_message_id=str(message.get("message_id") or ""),
                )
                payload["reprocess_goal_id"] = goal["goal_id"]
                payload["reprocess_recorded_at"] = utc_now()
                changed = True
        return changed

    def _reconcile_incomplete_goal_queue(self, state: dict[str, Any]) -> bool:
        changed = False
        for goal in state.get("goals", {}).values():
            if goal.get("archived_at"):
                continue
            if goal.get("completion_state") != "incomplete":
                continue
            goal_id = str(goal.get("goal_id") or "")
            if self._has_live_worker_signal_for_goal(state, goal_id):
                continue
            has_live_entry = any(
                entry.get("goal_id") == goal_id and entry.get("status") in {"queued", "acquired"}
                for entry in state.setdefault("dispatch_queue", [])
            )
            if has_live_entry:
                continue
            entry = self._enqueue_dispatch(
                state,
                goal,
                priority=DISPATCH_PRIORITY_GOAL,
                reason="Active incomplete Goal discovered.",
            )
            if entry:
                changed = True
        return changed

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
        state.setdefault("dispatch_queue", [])
        runtime_settings = state.setdefault("runtime_settings", {})
        state.setdefault("messages", [])
        state.setdefault("endpoint_cursors", {})
        state.setdefault("message_index", [])
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
        return changed

    def save(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.{new_id('tmp')}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
        tmp.replace(self.state_path)
