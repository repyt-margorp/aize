from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dispatch_policy import SCHEDULING_CLASS_SCORES
from model import Goal, Session, Unit, new_id, utc_now
from schedule_resolvers import ScheduleResolverError, resolve_next_run_at
from store_defs import (
    AGENT_ROLES,
    DEFAULT_SESSION_CAPABILITIES,
    GOAL_MANAGER_ROLE,
    ROOT_SESSION_ID,
    ROOT_USERNAME,
    WORKER_AGENT_ROLE,
    StoreError,
    account_endpoint,
    normalize_endpoint,
    session_endpoint,
)

UNIT_ACTIVATION_TRIGGERS = ("manual", "scheduled", "startup")


class SessionGoalMixin:
    def _safe_workspace_name(self, value: str, *, fallback: str) -> str:
        safe_value = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
        safe_value = safe_value.strip("-_") or fallback
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        return f"{safe_value}-{digest}"

    def _unit_workspace_relpath(self, unit_id: str) -> str:
        return f"workspaces/units/{self._safe_workspace_name(unit_id, fallback='unit')}"

    def _ensure_unit_workspace(self, unit: dict[str, Any]) -> bool:
        unit_id = str(unit.get("unit_id") or "").strip()
        if not unit_id:
            raise StoreError("unit_id is required for workspace creation")
        workspace_path = str(unit.get("workspace_path") or "").strip()
        if not workspace_path:
            workspace_path = self._unit_workspace_relpath(unit_id)
            unit["workspace_path"] = workspace_path
            changed = True
        else:
            changed = False
        workspace = Path(workspace_path)
        if workspace.is_absolute() or ".." in workspace.parts:
            raise StoreError(f"invalid unit workspace path: {workspace_path}")
        (self.root / workspace_path).mkdir(parents=True, exist_ok=True)
        return changed

    def _unit_workspace_abs_path(self, unit: dict[str, Any]) -> Path:
        self._ensure_unit_workspace(unit)
        return self.root / str(unit["workspace_path"])

    def _session_workspace_relpath(self, session_id: str) -> str:
        return f"workspaces/sessions/{self._safe_workspace_name(session_id, fallback='session')}"

    def _ensure_session_workspace(self, session: dict[str, Any]) -> bool:
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            raise StoreError("session_id is required for workspace creation")
        workspace_path = str(session.get("workspace_path") or "").strip()
        if not workspace_path:
            workspace_path = self._session_workspace_relpath(session_id)
            session["workspace_path"] = workspace_path
            changed = True
        else:
            changed = False
        workspace = Path(workspace_path)
        if workspace.is_absolute() or ".." in workspace.parts:
            raise StoreError(f"invalid session workspace path: {workspace_path}")
        (self.root / workspace_path).mkdir(parents=True, exist_ok=True)
        return changed

    def _session_workspace_abs_path(self, session: dict[str, Any]) -> Path:
        self._ensure_session_workspace(session)
        return self.root / str(session["workspace_path"])

    def _ensure_session_unit_workspace_link(self, session: dict[str, Any], unit: dict[str, Any] | None) -> bool:
        if unit is None:
            return False
        session_workspace = self._session_workspace_abs_path(session)
        unit_workspace = self._unit_workspace_abs_path(unit)
        link_path = session_workspace / "unit-workspace"
        target = os.path.relpath(unit_workspace, start=session_workspace)
        if link_path.is_symlink():
            if os.readlink(link_path) == target:
                return False
            link_path.unlink()
        elif link_path.exists():
            raise StoreError(f"reserved unit workspace link already exists: {link_path}")
        link_path.symlink_to(target, target_is_directory=True)
        return False

    def _ensure_session_metadata(self, state: dict[str, Any]) -> bool:
        changed = False
        for unit in state.get("units", {}).values():
            if self._ensure_unit_workspace(unit):
                changed = True
        for session in state.get("sessions", {}).values():
            if self._ensure_session_workspace(session):
                changed = True
            unit_id = str(session.get("unit_id") or "").strip()
            self._ensure_session_unit_workspace_link(session, state.get("units", {}).get(unit_id) if unit_id else None)
            if session.get("capabilities") != DEFAULT_SESSION_CAPABILITIES:
                session["capabilities"] = json.loads(json.dumps(DEFAULT_SESSION_CAPABILITIES, ensure_ascii=False))
                changed = True
            scheduling_policy = session.get("scheduling_policy")
            normalized_policy = {
                "class": str(scheduling_policy.get("class") or "normal") if isinstance(scheduling_policy, dict) else "normal",
                "base_priority": int(scheduling_policy.get("base_priority") or 0) if isinstance(scheduling_policy, dict) else 0,
            }
            if scheduling_policy != normalized_policy:
                session["scheduling_policy"] = normalized_policy
                changed = True
        return changed

    def create_unit(
        self,
        unit_id: str,
        *,
        instance_policy: str = "multi",
        display_name: str = "",
        description: str = "",
        goal_text: str = "",
        initial_prompt: str = "",
        schedule: dict[str, Any] | None = None,
        activation_triggers: dict[str, bool] | None = None,
        owner_account: str = ROOT_USERNAME,
    ) -> Unit:
        state = self.load()
        if instance_policy not in {"multi", "singleton"}:
            raise StoreError(f"unsupported instance policy: {instance_policy}")
        normalized_schedule = self._normalize_unit_schedule(schedule)
        normalized_triggers = self._normalize_unit_activation_triggers(
            activation_triggers,
            schedule=normalized_schedule,
        )
        units = state["units"]
        if unit_id in units:
            raise StoreError(f"unit already exists: {unit_id}")
        normalized_owner_account = str(owner_account or "").strip()
        if normalized_owner_account and normalized_owner_account not in state["accounts"]:
            raise StoreError(f"unknown owner account: {normalized_owner_account}")
        unit = Unit(
            unit_id=unit_id,
            created_at=utc_now(),
            instance_policy=instance_policy,
            display_name=str(display_name or "").strip(),
            description=str(description or "").strip(),
            goal_text=str(goal_text or "").strip(),
            initial_prompt=str(initial_prompt or "").strip(),
            schedule=normalized_schedule,
            activation_triggers=normalized_triggers,
            workspace_path=self._unit_workspace_relpath(unit_id),
            owner_account=normalized_owner_account,
        )
        units[unit_id] = unit.to_dict()
        self._ensure_unit_workspace(units[unit_id])
        self.save(state)
        return unit

    def configure_unit_schedule(
        self,
        unit_id: str,
        *,
        resolver: str,
        fixed_parameters: dict[str, Any],
        next_run_at: str | None = None,
        note: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        with self._state_lock():
            state = self.load()
            unit = state["units"].get(unit_id)
            if not unit:
                raise StoreError(f"unknown unit: {unit_id}")
            existing = unit.get("schedule")
            schedule = dict(existing) if isinstance(existing, dict) else {}
            schedule["enabled"] = bool(enabled)
            schedule["resolver"] = str(resolver or "").strip()
            schedule["fixed_parameters"] = dict(fixed_parameters)
            schedule.pop("last_resolution", None)
            if next_run_at is not None:
                schedule["next_run_at"] = str(next_run_at).strip()
            if note is not None:
                schedule["note"] = str(note).strip()
            unit["schedule"] = self._normalize_unit_schedule(schedule)
            self._backfill_unit_schedule_timing(state, unit)
            triggers = dict(unit.get("activation_triggers") or {})
            triggers["scheduled"] = bool(enabled)
            unit["activation_triggers"] = self._normalize_unit_activation_triggers(
                triggers,
                schedule=unit["schedule"],
            )
            unit["updated_at"] = utc_now()
            self.save(state)
            return dict(unit)

    def _backfill_unit_schedule_timing(self, state: dict[str, Any], unit: dict[str, Any]) -> None:
        schedule = unit.get("schedule")
        if not isinstance(schedule, dict) or schedule.get("resolver") != "next_interval_boundary":
            return
        fixed_parameters = schedule.get("fixed_parameters")
        if not isinstance(fixed_parameters, dict):
            return
        interval_seconds = int(fixed_parameters.get("interval_seconds") or 0)
        next_run_at = str(schedule.get("next_run_at") or "").strip()
        session_id = str(schedule.get("next_run_set_by_session_id") or "").strip()
        session = state.get("sessions", {}).get(session_id)
        if interval_seconds < 1 or not next_run_at or not session:
            return
        timing = session.setdefault("schedule_timing", {})
        if timing.get("scheduled_for"):
            return
        previous_boundary = self._parse_utc(next_run_at).timestamp() - interval_seconds
        timing["scheduled_for"] = datetime.fromtimestamp(previous_boundary, UTC).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")

    def _unit_launch_identity(
        self,
        state: dict[str, Any],
        *,
        unit: dict[str, Any],
        fallback_parent_session_id: str,
        fallback_created_by: str,
    ) -> tuple[str, str]:
        owner_account = str(unit.get("owner_account") or "").strip()
        account = state.get("accounts", {}).get(owner_account)
        if not account:
            return fallback_parent_session_id, fallback_created_by
        home_session_id = str(account.get("home_session_id") or "").strip()
        if home_session_id not in state.get("sessions", {}):
            raise StoreError(f"owner account has no home session: {owner_account}")
        return home_session_id, owner_account

    def run_scheduled_units(
        self,
        *,
        parent_session_id: str = ROOT_SESSION_ID,
        created_by: str = ROOT_USERNAME,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        state = self.load()
        if parent_session_id not in state["sessions"]:
            raise StoreError(f"unknown parent session: {parent_session_id}")
        if created_by not in state["accounts"]:
            raise StoreError(f"unknown account: {created_by}")
        now_text = now or utc_now()
        now_dt = self._parse_utc(now_text)
        started: list[dict[str, Any]] = []
        for unit in sorted(state["units"].values(), key=lambda item: str(item.get("unit_id") or "")):
            if unit.get("status") != "active":
                continue
            if not self._unit_activation_trigger_enabled(unit, "scheduled"):
                continue
            if not self._unit_schedule_due(unit, now_dt=now_dt):
                continue
            unit_id = str(unit.get("unit_id") or "")
            if not unit_id:
                continue
            unit_parent_session_id, unit_created_by = self._unit_launch_identity(
                state,
                unit=unit,
                fallback_parent_session_id=parent_session_id,
                fallback_created_by=created_by,
            )
            session_id = self._scheduled_session_id(state, unit_id=unit_id, now_text=now_text)
            scheduled_for = str(unit.get("schedule", {}).get("next_run_at") or "")
            label = str(unit.get("display_name") or unit_id).strip()
            goal_body = str(unit.get("goal_text") or label).strip()
            payload = self._start_goal_session_in_state(
                state,
                session_id=session_id,
                unit_id=unit_id,
                parent_session_ids=[unit_parent_session_id],
                label=label,
                body=goal_body,
                created_by=unit_created_by,
                created_at=now_text,
                activation_trigger="scheduled",
            )
            state["sessions"][session_id]["schedule_timing"] = {
                "scheduled_for": scheduled_for,
            }
            payload["session"] = dict(state["sessions"][session_id])
            initial_prompt = str(unit.get("initial_prompt") or "").strip()
            if initial_prompt:
                message = self._append_session_message_locked(
                    state,
                    session_id=session_id,
                    from_endpoint=account_endpoint(unit_created_by),
                    to_endpoint=session_endpoint(session_id),
                    payload={
                        "body": initial_prompt,
                        "user_input": True,
                        "scheduled_unit_id": unit_id,
                    },
                    created_at=now_text,
                )
                goal = state["goals"][payload["goal"]["goal_id"]]
                self._set_goal_completion_state(
                    state,
                    goal,
                    "incomplete",
                    reason=f"Scheduled Unit initial prompt {message['message_id']} requires Session processing.",
                    actor=unit_created_by,
                )
                message["payload"]["reprocess_goal_id"] = goal["goal_id"]
                message["payload"]["reprocess_recorded_at"] = now_text
                payload["initial_message"] = dict(message)
                payload["goal"] = dict(goal)
            unit["last_scheduled_at"] = now_text
            unit["updated_at"] = now_text
            unit["scheduled_run_count"] = int(unit.get("scheduled_run_count") or 0) + 1
            schedule = unit.get("schedule")
            if isinstance(schedule, dict):
                schedule["last_run_at"] = now_text
                schedule["next_run_at"] = ""
                schedule["next_run_required_by_session_id"] = session_id
            started.append(payload)
        self.save(state)
        return started

    def run_startup_units(
        self,
        *,
        parent_session_id: str = ROOT_SESSION_ID,
        created_by: str = ROOT_USERNAME,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        state = self.load()
        if parent_session_id not in state["sessions"]:
            raise StoreError(f"unknown parent session: {parent_session_id}")
        if created_by not in state["accounts"]:
            raise StoreError(f"unknown account: {created_by}")
        now_text = now or utc_now()
        started: list[dict[str, Any]] = []
        for unit in sorted(state["units"].values(), key=lambda item: str(item.get("unit_id") or "")):
            if unit.get("status") != "active":
                continue
            if not self._unit_activation_trigger_enabled(unit, "startup"):
                continue
            unit_id = str(unit.get("unit_id") or "")
            if not unit_id:
                continue
            unit_parent_session_id, unit_created_by = self._unit_launch_identity(
                state,
                unit=unit,
                fallback_parent_session_id=parent_session_id,
                fallback_created_by=created_by,
            )
            session_id = self._triggered_session_id(state, unit_id=unit_id, trigger="startup", now_text=now_text)
            label = str(unit.get("display_name") or unit_id).strip()
            goal_body = str(unit.get("goal_text") or label).strip()
            payload = self._start_goal_session_in_state(
                state,
                session_id=session_id,
                unit_id=unit_id,
                parent_session_ids=[unit_parent_session_id],
                label=label,
                body=goal_body,
                created_by=unit_created_by,
                created_at=now_text,
                activation_trigger="startup",
            )
            initial_prompt = str(unit.get("initial_prompt") or "").strip()
            if initial_prompt:
                message = self._append_session_message_locked(
                    state,
                    session_id=payload["session"]["session_id"],
                    from_endpoint=account_endpoint(unit_created_by),
                    to_endpoint=session_endpoint(payload["session"]["session_id"]),
                    payload={
                        "body": initial_prompt,
                        "user_input": True,
                        "startup_unit_id": unit_id,
                    },
                    created_at=now_text,
                )
                goal = state["goals"][payload["goal"]["goal_id"]]
                self._set_goal_completion_state(
                    state,
                    goal,
                    "incomplete",
                    reason=f"Startup Unit initial prompt {message['message_id']} requires Session processing.",
                    actor=unit_created_by,
                )
                message["payload"]["reprocess_goal_id"] = goal["goal_id"]
                message["payload"]["reprocess_recorded_at"] = now_text
                payload["initial_message"] = dict(message)
                payload["goal"] = dict(goal)
            unit["last_startup_at"] = now_text
            unit["updated_at"] = now_text
            unit["startup_run_count"] = int(unit.get("startup_run_count") or 0) + 1
            started.append(payload)
        self.save(state)
        return started

    def set_agent_provider(self, role: str, *, provider: str) -> dict[str, Any]:
        normalized_role = self._normalize_agent_role(role)
        if normalized_role not in AGENT_ROLES:
            raise StoreError(f"unsupported agent role: {role}")
        normalized_provider = provider.strip().lower()
        if normalized_provider not in {"local", "codex", "claude", "remote-aize"}:
            raise StoreError(f"unsupported agent provider: {provider}")
        if normalized_role == GOAL_MANAGER_ROLE and normalized_provider == "remote-aize":
            raise StoreError("GoalManager provider must be local to this PC")
        state = self.load()
        profile = state["agent_profiles"].setdefault(
            normalized_role,
            {
                "role": normalized_role,
                "created_at": utc_now(),
            },
        )
        profile["provider"] = normalized_provider
        profile["default_assignment"] = False
        profile["status"] = "active"
        profile["updated_at"] = utc_now()
        self.save(state)
        return dict(profile)

    def set_session_scheduling_policy(
        self,
        session_id: str,
        *,
        scheduling_class: str,
        base_priority: int,
    ) -> dict[str, Any]:
        normalized_class = str(scheduling_class or "").strip().lower()
        if normalized_class not in SCHEDULING_CLASS_SCORES:
            supported = ", ".join(sorted(SCHEDULING_CLASS_SCORES))
            raise StoreError(f"unsupported scheduling class: {scheduling_class}; expected one of {supported}")
        with self._state_lock():
            state = self.load()
            session = state["sessions"].get(session_id)
            if not session:
                raise StoreError(f"unknown session: {session_id}")
            policy = {"class": normalized_class, "base_priority": int(base_priority)}
            session["scheduling_policy"] = policy
            session["updated_at"] = utc_now()
            self.save(state)
            return dict(policy)

    def agent_profiles(self) -> list[dict[str, Any]]:
        state = self.load()
        return sorted(state["agent_profiles"].values(), key=lambda item: item["role"])

    def set_session_active(self, session_id: str, *, active: bool) -> dict[str, Any]:
        state = self.load()
        session = state["sessions"].get(session_id)
        if not session:
            raise StoreError(f"unknown session: {session_id}")
        now = utc_now()
        previous_active = bool(session.get("active", True))
        session["active"] = active
        session["updated_at"] = now
        self._log_session_active_change(
            state,
            session_id,
            previous_active=previous_active,
            active=active,
            actor="system",
            created_at=now,
        )
        self.save(state)
        return dict(session)

    def _normalize_agent_role(self, role: str) -> str:
        normalized = role.strip()
        aliases = {
            "goalmanager": GOAL_MANAGER_ROLE,
            "GoalManager": GOAL_MANAGER_ROLE,
            "workeragent": WORKER_AGENT_ROLE,
            "WorkerAgent": WORKER_AGENT_ROLE,
            "worker": WORKER_AGENT_ROLE,
        }
        return aliases.get(normalized, aliases.get(normalized.lower(), normalized))

    def create_session(
        self,
        session_id: str,
        *,
        unit_id: str | None = None,
        parent_session_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        normalized_unit_id = str(unit_id or "").strip() or None
        if normalized_unit_id and normalized_unit_id not in state["units"]:
            raise StoreError(f"unknown unit: {normalized_unit_id}")
        if normalized_unit_id and not self._unit_activation_trigger_enabled(state["units"][normalized_unit_id], "manual"):
            raise StoreError(f"unit does not allow manual activation: {normalized_unit_id}")
        sessions = state["sessions"]
        if session_id in sessions:
            raise StoreError(f"session already exists: {session_id}")
        normalized_parent_ids = parent_session_ids or [ROOT_SESSION_ID]
        if session_id != ROOT_SESSION_ID:
            for parent_session_id in normalized_parent_ids:
                if parent_session_id not in sessions:
                    raise StoreError(f"unknown parent session: {parent_session_id}")
        unit = state["units"].get(normalized_unit_id) if normalized_unit_id else None
        if unit and unit.get("instance_policy") == "singleton":
            singleton_session_id = unit.get("singleton_session_id")
            if singleton_session_id and singleton_session_id in sessions:
                raise StoreError(f"unit is singleton and already has session: {singleton_session_id}")
        now = utc_now()
        session = Session(
            session_id=session_id,
            unit_id=normalized_unit_id,
            created_at=now,
            updated_at=now,
            title=session_id,
        )
        sessions[session_id] = session.to_dict()
        self._ensure_role_cursors(sessions[session_id])
        self._ensure_session_workspace(sessions[session_id])
        self._ensure_session_unit_workspace_link(sessions[session_id], unit)
        if unit and unit.get("instance_policy") == "singleton":
            unit["singleton_session_id"] = session_id
            sessions[session_id]["singleton"] = True
        sessions[session_id]["capabilities"] = json.loads(json.dumps(DEFAULT_SESSION_CAPABILITIES, ensure_ascii=False))
        if session_id != ROOT_SESSION_ID:
            for parent_session_id in normalized_parent_ids:
                self._link_sessions_in_state(state, parent_session_id, session_id)
        self.save(state)
        return dict(sessions[session_id])

    def start_goal_session(
        self,
        session_id: str,
        *,
        unit_id: str | None = None,
        parent_session_ids: list[str],
        label: str,
        body: str = "",
        created_by: str,
    ) -> dict[str, Any]:
        session_title = label.strip()
        if not session_title:
            raise StoreError("session label is required")
        goal_body = str(body or "").strip() or session_title
        state = self.load()
        if created_by not in state["accounts"]:
            raise StoreError(f"unknown account: {created_by}")
        payload = self._start_goal_session_in_state(
            state,
            session_id=session_id,
            unit_id=unit_id,
            parent_session_ids=parent_session_ids,
            label=session_title,
            body=goal_body,
            created_by=created_by,
            created_at=utc_now(),
            activation_trigger="manual",
        )
        self.save(state)
        return payload

    def _start_goal_session_in_state(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        unit_id: str | None,
        parent_session_ids: list[str],
        label: str,
        body: str,
        created_by: str,
        created_at: str,
        activation_trigger: str = "manual",
    ) -> dict[str, Any]:
        session_title = label.strip()
        if not session_title:
            raise StoreError("session label is required")
        goal_body = str(body or "").strip() or session_title
        normalized_unit_id = str(unit_id or "").strip() or None
        if normalized_unit_id and normalized_unit_id not in state["units"]:
            raise StoreError(f"unknown unit: {normalized_unit_id}")
        if normalized_unit_id and not self._unit_activation_trigger_enabled(state["units"][normalized_unit_id], activation_trigger):
            raise StoreError(f"unit does not allow {activation_trigger} activation: {normalized_unit_id}")
        if session_id in state["sessions"]:
            raise StoreError(f"session already exists: {session_id}")
        unit = state["units"].get(normalized_unit_id) if normalized_unit_id else None
        if unit and unit.get("instance_policy") == "singleton":
            singleton_session_id = unit.get("singleton_session_id")
            if singleton_session_id and singleton_session_id in state["sessions"]:
                return self._activate_singleton_unit_session_in_state(
                    state,
                    unit=unit,
                    parent_session_ids=parent_session_ids,
                    label=session_title,
                    body=goal_body,
                    created_by=created_by,
                    created_at=created_at,
                    activation_trigger=activation_trigger,
                )
        normalized_parent_ids = parent_session_ids or [ROOT_SESSION_ID]
        for parent_session_id in normalized_parent_ids:
            if parent_session_id not in state["sessions"]:
                raise StoreError(f"unknown parent session: {parent_session_id}")

        session = Session(
            session_id=session_id,
            unit_id=normalized_unit_id,
            created_at=created_at,
            updated_at=created_at,
            title=session_title,
        )
        state["sessions"][session_id] = session.to_dict()
        self._ensure_role_cursors(state["sessions"][session_id])
        self._ensure_session_workspace(state["sessions"][session_id])
        self._ensure_session_unit_workspace_link(state["sessions"][session_id], unit)
        state["sessions"][session_id]["capabilities"] = json.loads(json.dumps(DEFAULT_SESSION_CAPABILITIES, ensure_ascii=False))
        if unit and unit.get("instance_policy") == "singleton":
            unit["singleton_session_id"] = session_id
            state["sessions"][session_id]["singleton"] = True
        for parent_session_id in normalized_parent_ids:
            self._link_sessions_in_state(state, parent_session_id, session_id)

        goal = Goal(
            goal_id=new_id("goal"),
            session_id=session_id,
            body=goal_body,
            created_by=created_by,
            created_at=created_at,
        )
        goal_dict = goal.to_dict()
        state["goals"][goal.goal_id] = goal_dict
        self._set_goal_completion_state(
            state,
            goal_dict,
            "incomplete",
            reason="Goal session created.",
            actor="system",
        )
        return {"session": dict(state["sessions"][session_id]), "goal": goal_dict}

    def _activate_singleton_unit_session_in_state(
        self,
        state: dict[str, Any],
        *,
        unit: dict[str, Any],
        parent_session_ids: list[str],
        label: str,
        body: str,
        created_by: str,
        created_at: str,
        activation_trigger: str,
    ) -> dict[str, Any]:
        unit_id = str(unit.get("unit_id") or "")
        session_id = str(unit.get("singleton_session_id") or unit_id)
        session = state["sessions"].get(session_id)
        if not session:
            session = Session(
                session_id=session_id,
                unit_id=unit_id,
                created_at=created_at,
                updated_at=created_at,
                title=str(label or unit_id).strip() or unit_id,
                singleton=True,
            ).to_dict()
            state["sessions"][session_id] = session
            unit["singleton_session_id"] = session_id
            self._ensure_role_cursors(session)
            self._ensure_session_workspace(session)
            self._ensure_session_unit_workspace_link(session, unit)
            session["capabilities"] = json.loads(json.dumps(DEFAULT_SESSION_CAPABILITIES, ensure_ascii=False))
        session["active"] = True
        session["updated_at"] = created_at
        normalized_parent_ids = parent_session_ids or [ROOT_SESSION_ID]
        for parent_session_id in normalized_parent_ids:
            if parent_session_id in state["sessions"] and parent_session_id != session_id:
                self._link_sessions_in_state(state, parent_session_id, session_id)
        goal = self._current_goal_for_session(state, session_id)
        goal_body = str(body or "").strip() or str(unit.get("goal_text") or label or unit_id).strip()
        if goal:
            if goal_body:
                goal["body"] = goal_body
                goal["updated_at"] = created_at
        else:
            goal = Goal(
                goal_id=new_id("goal"),
                session_id=session_id,
                body=goal_body or f"Run Unit {unit_id}.",
                created_by=created_by,
                created_at=created_at,
            ).to_dict()
            state["goals"][goal["goal_id"]] = goal
        signal = self._log_system_signal(
            state,
            session_id,
            signal_type="UnitActivated",
            body=f"Unit {unit_id} activated by {activation_trigger} trigger.",
            target_roles=[GOAL_MANAGER_ROLE],
            actor="system",
            data={
                "unit_id": unit_id,
                "activation_trigger": activation_trigger,
            },
            created_at=created_at,
        )
        self._set_goal_completion_state(
            state,
            goal,
            "incomplete",
            reason=f"Singleton Unit {unit_id} activated by {activation_trigger} trigger.",
            actor="system",
        )
        return {"session": dict(session), "goal": goal, "startup_signal" if activation_trigger == "startup" else "activation_signal": dict(signal)}

    def _normalize_unit_schedule(self, schedule: dict[str, Any] | None) -> dict[str, Any]:
        if not schedule:
            return {}
        enabled = bool(schedule.get("enabled"))
        next_run_at = str(schedule.get("next_run_at") or "").strip()
        resolver = str(schedule.get("resolver") or "explicit").strip()
        fixed_parameters = schedule.get("fixed_parameters")
        if not isinstance(fixed_parameters, dict):
            fixed_parameters = {}
        if schedule.get("every_hours") and resolver == "explicit":
            resolver = "next_interval_boundary"
            fixed_parameters = {
                "interval_seconds": int(schedule.get("every_hours") or 0) * 3600,
                "anchor": "scheduled_for",
            }
        normalized = {
            "enabled": enabled,
            "next_run_at": next_run_at,
            "note": str(schedule.get("note") or "").strip(),
            "resolver": resolver,
            "fixed_parameters": dict(fixed_parameters),
        }
        for key in (
            "last_run_at",
            "next_run_required_by_session_id",
            "next_run_note",
            "next_run_set_by_session_id",
            "next_run_set_by_run_id",
            "next_run_set_at",
            "last_resolution",
        ):
            if key in schedule:
                normalized[key] = schedule[key]
        if enabled and normalized["next_run_at"]:
            self._parse_utc(normalized["next_run_at"])
        try:
            if resolver not in {"explicit", "next_interval_boundary"}:
                resolve_next_run_at(resolver, {}, {})
        except ScheduleResolverError as exc:
            if "unknown schedule resolver" in str(exc):
                raise StoreError(str(exc)) from exc
        return normalized

    def _normalize_unit_activation_triggers(
        self,
        triggers: dict[str, Any] | None,
        *,
        schedule: dict[str, Any] | None,
    ) -> dict[str, bool]:
        schedule_enabled = isinstance(schedule, dict) and schedule.get("enabled") is True
        if not triggers:
            normalized = {
                "manual": True,
                "scheduled": bool(schedule_enabled),
                "startup": False,
            }
        else:
            normalized = {
                key: bool(triggers.get(key))
                for key in UNIT_ACTIVATION_TRIGGERS
            }
            if schedule_enabled and "scheduled" not in triggers:
                normalized["scheduled"] = True
        if not any(normalized.values()):
            raise StoreError("unit must allow at least one activation trigger")
        return normalized

    def _unit_activation_trigger_enabled(self, unit: dict[str, Any], trigger: str) -> bool:
        if trigger not in UNIT_ACTIVATION_TRIGGERS:
            raise StoreError(f"unsupported unit activation trigger: {trigger}")
        triggers = unit.get("activation_triggers")
        if not isinstance(triggers, dict):
            triggers = self._normalize_unit_activation_triggers(None, schedule=unit.get("schedule"))
            unit["activation_triggers"] = triggers
        return bool(triggers.get(trigger))

    def _unit_schedule_due(self, unit: dict[str, Any], *, now_dt: datetime) -> bool:
        schedule = unit.get("schedule")
        if not isinstance(schedule, dict) or schedule.get("enabled") is not True:
            return False
        next_run_at = str(schedule.get("next_run_at") or "").strip()
        if not next_run_at:
            return False
        return self._parse_utc(next_run_at) <= now_dt

    def _parse_utc(self, value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise StoreError(f"invalid UTC timestamp: {value}") from exc
        return parsed.astimezone(UTC)

    def set_next_unit_run_at_from_session(
        self,
        session_id: str,
        *,
        next_run_at: str,
        note: str = "",
        actor: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        return self.schedule_next_unit_run_from_session(
            session_id,
            call_parameters={"next_run_at": next_run_at, "note": note},
            actor=actor,
            run_id=run_id,
        )

    def schedule_next_unit_run_from_session(
        self,
        session_id: str,
        *,
        call_parameters: dict[str, Any],
        actor: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        with self._state_lock():
            state = self.load()
            result = self._schedule_next_unit_run_locked(
                state,
                session_id=session_id,
                call_parameters=call_parameters,
                actor=actor,
                run_id=run_id,
                completed_at=utc_now(),
                completion_is_provisional=True,
            )
            self.save(state)
            return result

    def _schedule_next_unit_run_locked(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        call_parameters: dict[str, Any],
        actor: str,
        run_id: str | None,
        completed_at: str,
        completion_is_provisional: bool = False,
    ) -> dict[str, Any]:
        session = state["sessions"].get(session_id)
        if not session:
            raise StoreError(f"unknown session: {session_id}")
        if self._normalize_agent_role(actor) != GOAL_MANAGER_ROLE:
            raise StoreError("only GoalManager may set a Scheduled Unit next run time")
        unit_id = str(session.get("unit_id") or "")
        if not unit_id:
            raise StoreError("session is not owned by a Unit")
        unit = state["units"].get(unit_id)
        if not unit:
            raise StoreError(f"session references unknown unit: {unit_id}")
        schedule = unit.get("schedule")
        if not isinstance(schedule, dict) or schedule.get("enabled") is not True:
            raise StoreError(f"unit is not scheduled: {unit_id}")
        runtime_parameters = self._schedule_runtime_parameters(
            state,
            session=session,
            completed_at=completed_at,
            call_parameters=call_parameters,
            completion_is_provisional=completion_is_provisional,
        )
        resolver = str(schedule.get("resolver") or "explicit")
        fixed_parameters = schedule.get("fixed_parameters")
        if not isinstance(fixed_parameters, dict):
            fixed_parameters = {}
        try:
            normalized_next = resolve_next_run_at(resolver, fixed_parameters, runtime_parameters)
        except ScheduleResolverError as exc:
            raise StoreError(str(exc)) from exc
        parsed_next = self._parse_utc(normalized_next)
        resolved_at = utc_now()
        comparison_time = self._parse_utc(completed_at or resolved_at)
        if parsed_next <= comparison_time:
            raise StoreError("next_run_at must be in the future")
        schedule["next_run_at"] = normalized_next
        schedule["next_run_note"] = str(call_parameters.get("note") or "").strip()
        schedule["next_run_set_by_session_id"] = session_id
        schedule["next_run_set_by_run_id"] = str(run_id or "")
        schedule["next_run_set_at"] = resolved_at
        schedule["last_resolution"] = {
            "resolver": resolver,
            "unit_parameters": dict(fixed_parameters),
            "runtime_parameters": dict(runtime_parameters),
            "next_run_at": normalized_next,
        }
        schedule.pop("next_run_required_by_session_id", None)
        unit["updated_at"] = schedule["next_run_set_at"]
        message = self._append_session_message_locked(
            state,
            session_id=session_id,
            from_endpoint=normalize_endpoint(GOAL_MANAGER_ROLE, session_id=session_id),
            to_endpoint=session_endpoint(session_id),
            payload={
                "body": f"Scheduled Unit next_run_at set to {normalized_next}.",
                "schedule_update": True,
                "unit_id": unit_id,
                "next_run_at": normalized_next,
                "note": str(call_parameters.get("note") or "").strip(),
                "resolver": resolver,
                "unit_parameters": dict(fixed_parameters),
                "runtime_parameters": dict(runtime_parameters),
                "run_id": str(run_id or ""),
            },
            created_at=schedule["next_run_set_at"],
        )
        return {"unit": dict(unit), "message": dict(message), "resolution": dict(schedule["last_resolution"])}

    def _schedule_runtime_parameters(
        self,
        state: dict[str, Any],
        *,
        session: dict[str, Any],
        completed_at: str,
        call_parameters: dict[str, Any],
        completion_is_provisional: bool,
    ) -> dict[str, Any]:
        session_id = str(session.get("session_id") or "")
        timing = session.get("schedule_timing")
        if not isinstance(timing, dict):
            timing = {}
        started_at = str(timing.get("started_at") or "")
        if not started_at:
            run_starts = sorted(
                str(run.get("lease_acquired_at") or "")
                for run in state.setdefault("dispatch_runs", {}).values()
                if run.get("session_id") == session_id and run.get("lease_acquired_at")
            )
            started_at = run_starts[0] if run_starts else ""
        return {
            "session_id": session_id,
            "unit_id": str(session.get("unit_id") or ""),
            "scheduled_for": str(timing.get("scheduled_for") or ""),
            "queued_at": str(timing.get("queued_at") or ""),
            "started_at": started_at,
            "completed_at": completed_at,
            "completion_is_provisional": completion_is_provisional,
            "call_parameters": dict(call_parameters),
        }

    def _scheduled_session_id(self, state: dict[str, Any], *, unit_id: str, now_text: str) -> str:
        return self._triggered_session_id(state, unit_id=unit_id, trigger="scheduled", now_text=now_text)

    def _triggered_session_id(self, state: dict[str, Any], *, unit_id: str, trigger: str, now_text: str) -> str:
        safe_unit_id = "".join(char if char.isalnum() else "-" for char in unit_id.lower()).strip("-")
        safe_time = "".join(char if char.isalnum() else "" for char in now_text.lower())
        safe_trigger = "".join(char if char.isalnum() else "-" for char in trigger.lower()).strip("-") or "trigger"
        base = f"{safe_unit_id}-{safe_trigger}-{safe_time or new_id(safe_trigger)}"
        candidate = base
        index = 2
        while candidate in state["sessions"]:
            candidate = f"{base}-{index}"
            index += 1
        return candidate

    def update_goal(
        self,
        session_id: str,
        *,
        body: str,
        created_by: str,
    ) -> dict[str, Any]:
        normalized_body = str(body or "").strip()
        if not normalized_body:
            raise StoreError("goal body is required")
        state = self.load()
        if session_id not in state["sessions"]:
            raise StoreError(f"unknown session: {session_id}")
        if created_by not in state["accounts"]:
            raise StoreError(f"unknown account: {created_by}")
        now = utc_now()
        goal_dict = self._current_goal_for_session(state, session_id)
        reason = "SessionGoal updated."
        if goal_dict:
            goal_dict["body"] = normalized_body
            goal_dict["created_by"] = created_by
            goal_dict["updated_at"] = now
        else:
            goal = Goal(
                goal_id=new_id("goal"),
                session_id=session_id,
                body=normalized_body,
                created_by=created_by,
                created_at=now,
            )
            goal_dict = goal.to_dict()
            state["goals"][goal.goal_id] = goal_dict
            reason = "SessionGoal created."
        state["sessions"][session_id]["updated_at"] = now
        self._set_goal_completion_state(
            state,
            goal_dict,
            "incomplete",
            reason=reason,
            actor="system",
        )
        self.save(state)
        return goal_dict

    def _set_goal_completion_state(
        self,
        state: dict[str, Any],
        goal: dict[str, Any],
        completion_state: str,
        *,
        reason: str,
        actor: str,
        run_id: str | None = None,
        occurred_at: str | None = None,
        defer_goal_manager: bool = False,
    ) -> dict[str, Any]:
        if completion_state not in {"complete", "incomplete"}:
            raise StoreError(f"unsupported goal completion state: {completion_state}")
        now = occurred_at or utc_now()
        previous_state = str(goal.get("completion_state") or "incomplete")
        goal["completion_state"] = completion_state
        goal["completion_reason"] = reason
        goal["completion_reason_updated_at"] = now
        if completion_state == "complete":
            goal["completed_at"] = now
            self._resolve_dispatch_readiness(state, str(goal.get("goal_id") or ""), resolved_at=now)
        else:
            goal.pop("completed_at", None)
            goal["last_incomplete_at"] = now
        session = state["sessions"].get(str(goal.get("session_id") or ""))
        if session:
            session["updated_at"] = now
        transition = {
            "previous_state": previous_state,
            "completion_state": completion_state,
            "reason": reason,
            "actor": normalize_endpoint(actor, session_id=str(goal.get("session_id") or "")),
            "run_id": run_id,
            "created_at": now,
        }
        if run_id:
            transition["run_id"] = run_id
        if defer_goal_manager:
            transition["defer_goal_manager_until_worker_report"] = True
        self._log_goal_state_transition(state, goal, transition)
        return transition

    def set_goal_completion_state_from_runtime(
        self,
        session_id: str,
        *,
        completion_state: str,
        reason: str,
        actor: str,
        run_id: str | None,
        schedule_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._normalize_agent_role(actor) != GOAL_MANAGER_ROLE:
            raise StoreError("only GoalManager may set a SessionGoal completion state")
        if completion_state not in {"complete", "incomplete"}:
            raise StoreError(f"unsupported goal completion state: {completion_state}")
        if not run_id:
            raise StoreError("GoalManager completion state requires an active dispatch run")
        with self._state_lock():
            state = self.load()
            run = state.setdefault("dispatch_runs", {}).get(run_id)
            if not run:
                raise StoreError(f"unknown dispatch run: {run_id}")
            if (
                str(run.get("session_id") or "") != session_id
                or str(run.get("role") or "") != GOAL_MANAGER_ROLE
                or run.get("lease_state") != "acquired"
            ):
                raise StoreError("GoalManager completion state requires its acquired dispatch lease")
            goal = self._current_goal_for_session(state, session_id)
            if not goal or str(goal.get("goal_id") or "") != str(run.get("goal_id") or ""):
                raise StoreError("dispatch run no longer owns the current SessionGoal")
            if isinstance(run.get("declared_outcome"), dict):
                raise StoreError("GoalManager dispatch run already declared its completion outcome")
            declaration = {
                "completion_state": completion_state,
                "reason": str(reason or "").strip() or "GoalManager recorded a completion decision.",
                "schedule_parameters": dict(schedule_parameters or {}),
                "declared_at": utc_now(),
            }
            run["declared_outcome"] = declaration
            self.save(state)
            return dict(declaration)

    def _current_goal_for_session(self, state: dict[str, Any], session_id: str) -> dict[str, Any] | None:
        session_goals = sorted(
            [
                goal
                for goal in state.get("goals", {}).values()
                if str(goal.get("session_id") or "") == session_id and not goal.get("archived_at")
            ],
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("goal_id") or "")),
        )
        return session_goals[-1] if session_goals else None

    def _current_goals(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        by_session: dict[str, dict[str, Any]] = {}
        for goal in sorted(
            [
                goal
                for goal in state.get("goals", {}).values()
                if not goal.get("archived_at")
            ],
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("goal_id") or "")),
        ):
            session_id = str(goal.get("session_id") or "")
            if session_id:
                by_session[session_id] = goal
        return [goal for session_id, goal in sorted(by_session.items())]

    def _mark_session_reprocess_needed(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        actor: str,
        reason: str,
        created_at: str,
        defer_goal_manager: bool = False,
    ) -> dict[str, Any]:
        target_goal = self._current_goal_for_session(state, session_id)
        if not target_goal:
            target_goal = Goal(
                goal_id=new_id("goal"),
                session_id=session_id,
                body="Reply to user input sent to this Session.",
                created_by=actor,
                created_at=created_at,
            ).to_dict()
            state["goals"][target_goal["goal_id"]] = target_goal
        self._set_goal_completion_state(
            state,
            target_goal,
            "incomplete",
            reason=reason,
            actor=actor,
            defer_goal_manager=defer_goal_manager,
        )
        return target_goal

    def link_sessions(self, parent_session_id: str, child_session_id: str) -> dict[str, Any]:
        state = self.load()
        edge = self._link_sessions_in_state(state, parent_session_id, child_session_id)
        self.save(state)
        return edge

    def _link_sessions_in_state(
        self,
        state: dict[str, Any],
        parent_session_id: str,
        child_session_id: str,
    ) -> dict[str, Any]:
        sessions = state["sessions"]
        if parent_session_id not in sessions:
            raise StoreError(f"unknown parent session: {parent_session_id}")
        if child_session_id not in sessions:
            raise StoreError(f"unknown child session: {child_session_id}")
        if parent_session_id == child_session_id:
            raise StoreError("session graph cannot contain self edges")

        edges = state["session_edges"]
        for edge in edges:
            if (
                edge.get("parent_session_id") == parent_session_id
                and edge.get("child_session_id") == child_session_id
            ):
                return dict(edge)

        if self._has_path(edges, child_session_id, parent_session_id):
            raise StoreError(f"link would create a cycle: {parent_session_id} -> {child_session_id}")

        edge = {
            "parent_session_id": parent_session_id,
            "child_session_id": child_session_id,
            "created_at": utc_now(),
        }
        edges.append(edge)
        return dict(edge)

    def _has_path(self, edges: list[dict[str, Any]], start: str, target: str) -> bool:
        children_by_parent: dict[str, list[str]] = {}
        for edge in edges:
            children_by_parent.setdefault(str(edge.get("parent_session_id")), []).append(
                str(edge.get("child_session_id"))
            )
        stack = [start]
        seen: set[str] = set()
        while stack:
            session_id = stack.pop()
            if session_id == target:
                return True
            if session_id in seen:
                continue
            seen.add(session_id)
            stack.extend(children_by_parent.get(session_id, []))
        return False
