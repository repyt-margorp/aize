from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from typing import Any

from goal.agents import AgentError, AgentRunner, provider_is_available
from kernel.model import new_id, utc_now
from goal.defs import (
    DEFAULT_AGENT_PROVIDER,
    GOAL_MANAGER_ROLE,
    WORKER_AGENT_ROLE,
    agent_sender_endpoint,
    normalize_endpoint,
    slot_group_name,
)
from kernel.defs import (
    ROOT_USERNAME,
    SESSION_RECIPIENT,
    StoreError,
    node_endpoint,
    user_endpoint,
)


class DispatchMixin:
    def dispatch_once(
        self,
        *,
        session_id: str | None = None,
        recovery_context: str | None = None,
        dispatch_lot_id: int | None = None,
    ) -> dict[str, Any] | None:
        with self._state_lock():
            lease = self._acquire_dispatch_lease_locked(
                session_id=session_id,
                recovery_context=recovery_context,
                dispatch_lot_id=dispatch_lot_id,
            )
        if lease is None:
            return None
        try:
            runner = AgentRunner()
            goal = lease["goal"]
            session = lease["session"]
            unit = lease["unit"]
            run = lease["run"]
            run_id = run["run_id"]
            session_messages = lease["session_messages"]
            role = lease["role"]
            agent_profile = lease["agent_profile"]
            provider = str(lease.get("provider") or agent_profile.get("provider") or "local")
            agent_user = str(lease.get("agent_user") or "")
            agent_group = str(lease.get("agent_group") or "")

            if role == GOAL_MANAGER_ROLE:
                with self._state_lock():
                    run_messages = self._messages_for_run_locked(
                        self.load(),
                        session_id=str(session["session_id"]),
                        run_id=run_id,
                    )
                prompt = self._render_goal_manager_prompt(
                    goal,
                    session,
                    unit,
                    phase="review",
                    session_messages=session_messages,
                    dispatch_messages=lease["dispatch_messages"],
                    dispatch_signals=lease["dispatch_signals"],
                    run_messages=run_messages,
                    log_window=run,
                    recovery_context=lease.get("recovery_context") or "",
                )
                agent_result = runner.run(
                    provider,
                    role=GOAL_MANAGER_ROLE,
                    prompt=prompt,
                    resume_token=lease["resume_token"],
                    provider_thread=lease["agent_thread"].get("provider_threads"),
                    cwd=self._session_workspace_abs_path(session),
                    runtime_env=self._agent_runtime_env(
                        session_id=str(session["session_id"]),
                        role=GOAL_MANAGER_ROLE,
                        run_id=run_id,
                        session=session,
                        unit=unit,
                        agent_user=agent_user,
                        agent_group=agent_group,
                    ),
                )
                result = {
                    "prompt": prompt,
                    "agent_result": agent_result,
                }
                with self._state_lock():
                    return self._commit_goal_manager_run_locked(lease, result)

            if role == WORKER_AGENT_ROLE:
                prompt = self._render_worker_prompt(
                    goal,
                    session,
                    unit,
                    session_messages=session_messages,
                    dispatch_messages=lease["dispatch_messages"],
                    dispatch_signals=lease["dispatch_signals"],
                    log_window=run,
                    recovery_context=lease.get("recovery_context") or "",
                )
                agent_result = runner.run(
                    provider,
                    role=WORKER_AGENT_ROLE,
                    prompt=prompt,
                    resume_token=lease["resume_token"],
                    provider_thread=lease["agent_thread"].get("provider_threads"),
                    cwd=self._session_workspace_abs_path(session),
                    runtime_env=self._agent_runtime_env(
                        session_id=str(session["session_id"]),
                        role=WORKER_AGENT_ROLE,
                        run_id=run_id,
                        session=session,
                        unit=unit,
                        agent_user=agent_user,
                        agent_group=agent_group,
                    ),
                )
                result = {
                    "prompt": prompt,
                    "agent_result": agent_result,
                    "dispatch_messages": lease["dispatch_messages"],
                }
                with self._state_lock():
                    return self._commit_worker_run_locked(lease, result)

            raise StoreError(f"unsupported dispatch role: {role}")
        except AgentError as exc:
            with self._state_lock():
                return self._commit_dispatch_error_locked(lease, exc)

    def _acquire_dispatch_lease_locked(
        self,
        *,
        session_id: str | None = None,
        recovery_context: str | None = None,
        dispatch_lot_id: int | None = None,
    ) -> dict[str, Any] | None:
        state = self.load()
        if session_id and session_id not in state["sessions"]:
            raise StoreError(f"unknown session: {session_id}")
        liveness_changed = self._reconcile_dispatch_liveness(state)
        readiness_entry = self._next_dispatch_readiness_entry(state, session_id=session_id)
        if not readiness_entry:
            if liveness_changed:
                self.save(state)
            return None

        goal = state["goals"][str(readiness_entry.get("goal_id") or "")]
        actual_session_id = str(goal.get("session_id") or "")
        session = state["sessions"].get(actual_session_id)
        if not session:
            raise StoreError(f"goal references unknown session: {actual_session_id}")
        if session.get("active") is not True:
            raise StoreError(f"goal session is inactive: {actual_session_id}")
        unit_id = str(session.get("unit_id") or "").strip()
        unit = state["units"].get(unit_id) if unit_id else None
        if unit_id and not unit:
            raise StoreError(f"session references unknown unit: {unit_id}")
        if unit and unit.get("status") != "active":
            raise StoreError(f"session unit is not active: {unit_id}")

        now = utc_now()
        scheduling_score = readiness_entry.pop("_scheduling_score", None)
        scheduling_reason = str(readiness_entry.pop("_scheduling_reason", ""))
        readiness_entry["status"] = "acquired"
        readiness_entry["acquired_at"] = now
        schedule_timing = session.get("schedule_timing")
        if isinstance(schedule_timing, dict):
            if not schedule_timing.get("queued_at"):
                schedule_timing["queued_at"] = str(readiness_entry.get("first_ready_at") or now)
            if not schedule_timing.get("started_at"):
                schedule_timing["started_at"] = now
        role = str(readiness_entry.get("role") or GOAL_MANAGER_ROLE)
        if role not in {GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE}:
            raise StoreError(f"unsupported dispatch role: {role}")
        agent_profile = state["agent_profiles"].get(role, {"provider": DEFAULT_AGENT_PROVIDER})
        appointed = self._select_dispatch_account(state, role=role, profile=agent_profile)
        provider = str((appointed or {}).get("provider") or agent_profile.get("provider") or DEFAULT_AGENT_PROVIDER)
        agent_user = str((appointed or {}).get("username") or "")
        agent_thread = self._ensure_agent_thread(
            state,
            session_id=actual_session_id,
            role=role,
            account=agent_user,
            provider=provider,
        )
        session_messages = self._session_messages_for_dispatch(state, session_id=actual_session_id)
        dispatch_messages = self._dispatch_messages_for_readiness_locked(
            state,
            session_id=actual_session_id,
            readiness_entry=readiness_entry,
        )
        dispatch_signals = self._dispatch_signals_for_readiness_locked(
            state,
            session_id=actual_session_id,
            role=role,
            readiness_entry=readiness_entry,
        )
        run_id = new_id("run")
        run = {
            "run_id": run_id,
            "goal_id": goal["goal_id"],
            "session_id": actual_session_id,
            "role": role,
            "unit_id": unit_id,
            "lease_state": "acquired",
            "readiness_id": readiness_entry["readiness_id"],
            "scheduling_score": scheduling_score,
            "scheduling_reason": scheduling_reason,
            "created_at": now,
            "lease_acquired_at": now,
            "steps": [],
            "session_message_ids": [message["message_id"] for message in session_messages],
            "current_phase": role,
            "agent_group": slot_group_name(role),
            "agent_user": agent_user,
            "agent_provider": provider,
            "observed_goal_body": str(goal.get("body") or ""),
            "from_log_seq": readiness_entry.get("from_log_seq"),
            "observed_to_seq": readiness_entry.get("observed_to_seq"),
            "wake_reasons": [
                dict(item)
                for item in readiness_entry.get("wake_reasons", [])
                if isinstance(item, dict)
            ],
        }
        if dispatch_lot_id is not None:
            run["dispatch_lot_id"] = int(dispatch_lot_id)
        if recovery_context:
            run["recovery_context"] = recovery_context
        state["dispatch_runs"][run_id] = run
        goal["dispatched_at"] = now
        if unit_id:
            goal["dispatched_unit_id"] = unit_id
        self.save(state)
        return {
            "goal": dict(goal),
            "session": dict(session),
            "unit": dict(unit) if unit else None,
            "run": dict(run),
            "readiness_id": readiness_entry["readiness_id"],
            "readiness_entry": dict(readiness_entry),
            "session_messages": [dict(message) for message in session_messages],
            "dispatch_messages": [dict(message) for message in dispatch_messages],
            "dispatch_signals": [dict(signal) for signal in dispatch_signals],
            "role": role,
            "agent_profile": dict(agent_profile),
            "provider": provider,
            "agent_user": agent_user,
            "agent_group": slot_group_name(role),
            "agent_thread": dict(agent_thread),
            "resume_token": str(agent_thread["resume_token"]),
            "recovery_context": recovery_context or "",
        }

    def _commit_goal_manager_run_locked(self, lease: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        run = state["dispatch_runs"].get(lease["run"]["run_id"])
        if run is None:
            return self._recover_missing_lease_run_locked(state, lease)
        goal = state["goals"][lease["goal"]["goal_id"]]
        session = state["sessions"][lease["session"]["session_id"]]
        fence_reason = self._stale_dispatch_fence_locked(state, run, role=GOAL_MANAGER_ROLE)
        if fence_reason:
            return self._discard_stale_dispatch_result_locked(
                state,
                run,
                lease=lease,
                role=GOAL_MANAGER_ROLE,
                fence_reason=fence_reason,
                result=result,
            )
        readiness_entry = self._readiness_entry_by_id(state, lease["readiness_id"])
        thread = self._ensure_agent_thread(
            state,
            session_id=session["session_id"],
            role=GOAL_MANAGER_ROLE,
            account=str(run.get("agent_user") or ""),
            provider=str(run.get("agent_provider") or ""),
        )
        self._record_agent_turn(
            thread,
            prompt=result["prompt"],
            result=result["agent_result"].output,
            provider=result["agent_result"].provider,
            provider_thread_id=result["agent_result"].provider_thread_id,
        )
        self._append_dispatch_step_message(
            state,
            run,
            session_id=session["session_id"],
            sender=GOAL_MANAGER_ROLE,
            recipient="Session",
            body=result["agent_result"].output,
            provider=result["agent_result"].provider,
            phase="GoalManagerReview",
        )

        completed_at = utc_now()
        decision = self._declared_goal_outcome_for_run(
            state,
            run_id=run["run_id"],
        )
        if decision is None:
            if readiness_entry:
                readiness_entry["status"] = "resolved"
                readiness_entry["resolved_at"] = completed_at
            self._set_role_cursor(
                state,
                session_id=session["session_id"],
                role=GOAL_MANAGER_ROLE,
                seq=int(run.get("observed_to_seq") or 0),
            )
            run["completion_state"] = "incomplete"
            run["goal_manager_status"] = "missing_completion_decision"
            protocol_signal = self._log_system_signal(
                state,
                session["session_id"],
                signal_type="GoalManagerTurnProtocolViolation",
                body=(
                    "GoalManager returned without declaring the SessionGoal complete or incomplete. "
                    "Review the unchanged SessionGoal and produce the required completion decision."
                ),
                target_roles=[GOAL_MANAGER_ROLE],
                actor="dispatcher",
                run_id=run["run_id"],
                data={
                    "required_output": "goal_completion_state",
                    "available_after": self._role_turn_retry_at(),
                },
                created_at=completed_at,
            )
            self._record_role_turn_outcome(
                state,
                run,
                session_id=session["session_id"],
                role=GOAL_MANAGER_ROLE,
                goal_decision=None,
                protocol_signal=protocol_signal,
            )
            run["lease_state"] = "released"
            run["lease_released_at"] = completed_at
            run.pop("current_phase", None)
            self._mark_session_messages_delivered(state, lease["session_messages"], delivered_at=completed_at)
            session["updated_at"] = completed_at
            self.save(state)
            return {
                "goal": dict(goal),
                "session": dict(session),
                "unit": dict(lease["unit"]) if lease["unit"] else None,
                "run": dict(run),
                "state_transition": None,
                "message": None,
            }
        worker_requested = self._has_live_worker_readiness_for_goal(
            state,
            str(goal.get("goal_id") or ""),
        ) or self._has_worker_request_for_run(state, session_id=session["session_id"], run_id=run["run_id"])
        implicit_worker_message = None
        if decision["completion_state"] == "waiting":
            wait_reason = str(decision.get("reason") or "")
            goal["waiting"] = {
                **dict(decision.get("wait") or {}),
                "reason": wait_reason,
                "declared_at": completed_at,
                "declared_run_id": run["run_id"],
            }
            transition = None
            self._log_goal_state_transition(
                state,
                goal,
                {
                    "completion_state": "waiting",
                    "reason": wait_reason,
                    "actor": GOAL_MANAGER_ROLE,
                    "run_id": run["run_id"],
                    "created_at": completed_at,
                },
            )
            self._resolve_goal_manager_readiness_for_goal(
                state,
                str(goal.get("goal_id") or ""),
                resolved_at=completed_at,
            )
            run["completion_state"] = "incomplete"
            run["goal_manager_status"] = "waiting"
        elif decision["completion_state"] == "complete":
            schedule_reason = self._prepare_scheduled_unit_completion(
                state,
                session=session,
                run=run,
                decision=decision,
                completed_at=completed_at,
            )
            if schedule_reason:
                self._log_system_signal(
                    state,
                    session["session_id"],
                    signal_type="GoalCompletionRejected",
                    body=schedule_reason,
                    target_roles=[GOAL_MANAGER_ROLE],
                    actor="scheduler",
                    run_id=run["run_id"],
                    data={
                        "goal_id": goal["goal_id"],
                        "unit_id": str(session.get("unit_id") or ""),
                        "rejected_completion_state": "complete",
                    },
                    created_at=completed_at,
                )
                transition = self._set_goal_completion_state(
                    state,
                    goal,
                    "incomplete",
                    reason=schedule_reason,
                    actor="scheduler",
                    run_id=run["run_id"],
                )
                run["completion_state"] = "incomplete"
                run["goal_manager_status"] = "schedule_next_run_required"
            else:
                transition = self._set_goal_completion_state(
                    state,
                    goal,
                    "complete",
                    reason=str(decision.get("reason") or ""),
                    actor=GOAL_MANAGER_ROLE,
                    run_id=run["run_id"],
                    occurred_at=completed_at,
                )
                session.setdefault("schedule_timing", {})["completed_at"] = completed_at
                run["completion_state"] = "complete"
        else:
            transition = self._set_goal_completion_state(
                state,
                goal,
                "incomplete",
                reason=str(decision.get("reason") or ""),
                actor=GOAL_MANAGER_ROLE,
                run_id=run["run_id"],
            )
            if not worker_requested:
                implicit_worker_message = self._append_implicit_worker_request_for_incomplete_goal(
                    state,
                    goal=goal,
                    session=session,
                    run=run,
                    reason=str(decision.get("reason") or ""),
                    created_at=completed_at,
                )
                worker_requested = True
            if worker_requested:
                self._resolve_goal_manager_readiness_for_goal(
                    state,
                    str(goal.get("goal_id") or ""),
                    resolved_at=completed_at,
                )
            run["completion_state"] = "incomplete"
            run["goal_manager_status"] = "incomplete"
        self._record_role_turn_outcome(
            state,
            run,
            session_id=session["session_id"],
            role=GOAL_MANAGER_ROLE,
            goal_decision=decision,
        )
        if readiness_entry and (
            run.get("completion_state") == "complete"
            or run.get("goal_manager_status") in {"schedule_next_run_required", "waiting"}
        ):
            readiness_entry["status"] = "resolved"
            readiness_entry["resolved_at"] = completed_at
        self._set_role_cursor(
            state,
            session_id=session["session_id"],
            role=GOAL_MANAGER_ROLE,
            seq=int(run.get("observed_to_seq") or 0),
        )
        run["lease_state"] = "released"
        run["lease_released_at"] = completed_at
        run.pop("current_phase", None)
        self._mark_session_messages_delivered(state, lease["session_messages"], delivered_at=completed_at)
        session["updated_at"] = completed_at
        self.save(state)
        return {
            "goal": dict(goal),
            "session": dict(session),
            "unit": dict(lease["unit"]) if lease["unit"] else None,
            "run": dict(run),
            "state_transition": transition,
            "message": dict(implicit_worker_message) if implicit_worker_message else None,
        }

    def _scheduled_unit_completion_blocker(
        self,
        state: dict[str, Any],
        *,
        session: dict[str, Any],
        completed_at: str,
    ) -> str:
        unit_id = str(session.get("unit_id") or "")
        if not unit_id:
            return ""
        unit = state.get("units", {}).get(unit_id)
        if not unit:
            return ""
        schedule = unit.get("schedule")
        if not isinstance(schedule, dict) or schedule.get("enabled") is not True:
            return ""
        next_run_at = str(schedule.get("next_run_at") or "").strip()
        if not next_run_at:
            return (
                "Scheduled Unit completion requires a future next_run_at. "
                "Call set_goal_completion_state(..., schedule_parameters={...}) so the Unit schedule resolver can calculate it."
            )
        try:
            next_dt = self._parse_utc(next_run_at)
            completed_dt = self._parse_utc(completed_at)
        except StoreError:
            return (
                "Scheduled Unit completion requires a valid future Unit next_run_at. "
                "Call set_goal_completion_state(..., schedule_parameters={...}) so the Unit schedule resolver can calculate it."
            )
        if next_dt <= completed_dt:
            return (
                f"Scheduled Unit next_run_at {next_run_at} is not in the future. "
                "Call set_goal_completion_state(..., schedule_parameters={...}) so the Unit schedule resolver can calculate it."
            )
        return ""

    def _prepare_scheduled_unit_completion(
        self,
        state: dict[str, Any],
        *,
        session: dict[str, Any],
        run: dict[str, Any],
        decision: dict[str, Any],
        completed_at: str,
    ) -> str:
        unit_id = str(session.get("unit_id") or "")
        unit = state.get("units", {}).get(unit_id) if unit_id else None
        schedule = unit.get("schedule") if unit else None
        if not isinstance(schedule, dict) or schedule.get("enabled") is not True:
            return ""
        next_run_at = str(schedule.get("next_run_at") or "").strip()
        resolver = str(schedule.get("resolver") or "explicit")
        resolve_next = resolver != "explicit" or not next_run_at
        if next_run_at and not resolve_next:
            try:
                resolve_next = self._parse_utc(next_run_at) <= self._parse_utc(completed_at)
            except StoreError:
                resolve_next = True
        if resolve_next:
            schedule_parameters = decision.get("schedule_parameters")
            try:
                self._schedule_next_unit_run_locked(
                    state,
                    session_id=str(session["session_id"]),
                    call_parameters=dict(schedule_parameters) if isinstance(schedule_parameters, dict) else {},
                    actor=GOAL_MANAGER_ROLE,
                    run_id=str(run["run_id"]),
                    completed_at=completed_at,
                    completion_is_provisional=False,
                )
            except StoreError as exc:
                return f"Scheduled Unit next run resolution failed: {exc}"
        return self._scheduled_unit_completion_blocker(state, session=session, completed_at=completed_at)

    def _commit_worker_run_locked(self, lease: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        run = state["dispatch_runs"].get(lease["run"]["run_id"])
        if run is None:
            return self._recover_missing_lease_run_locked(state, lease)
        goal = state["goals"][lease["goal"]["goal_id"]]
        session = state["sessions"][lease["session"]["session_id"]]
        fence_reason = self._stale_dispatch_fence_locked(state, run, role=WORKER_AGENT_ROLE)
        if fence_reason:
            return self._discard_stale_dispatch_result_locked(
                state,
                run,
                lease=lease,
                role=WORKER_AGENT_ROLE,
                fence_reason=fence_reason,
                result=result,
            )
        readiness_entry = self._readiness_entry_by_id(state, lease["readiness_id"])
        thread = self._ensure_agent_thread(
            state,
            session_id=session["session_id"],
            role=WORKER_AGENT_ROLE,
            account=str(run.get("agent_user") or ""),
            provider=str(run.get("agent_provider") or ""),
        )
        self._record_agent_turn(
            thread,
            prompt=result["prompt"],
            result=result["agent_result"].output,
            provider=result["agent_result"].provider,
            provider_thread_id=result["agent_result"].provider_thread_id,
        )
        self._append_dispatch_step_message(
            state,
            run,
            session_id=session["session_id"],
            sender=WORKER_AGENT_ROLE,
            recipient="Session",
            body=result["agent_result"].output,
            provider=result["agent_result"].provider,
            phase="WorkerWork",
        )
        if result["agent_result"].provider == "remote-aize":
            self._append_remote_aize_handoff_message(
                state,
                run,
                session_id=session["session_id"],
                goal=goal,
                worker_prompt=result["prompt"],
            )
        completed_at = utc_now()
        worker_reports = self._role_turn_session_messages(
            state,
            session_id=session["session_id"],
            run_id=run["run_id"],
            run=run,
            role=WORKER_AGENT_ROLE,
        )
        protocol_signal = None
        if not worker_reports:
            protocol_signal = self._log_system_signal(
                state,
                session["session_id"],
                signal_type="WorkerTurnProtocolViolation",
                body=(
                    "WorkerAgent returned without reporting work results to Session. "
                    "GoalManager must review the failed Worker turn and decide whether to request the work again."
                ),
                target_roles=[GOAL_MANAGER_ROLE],
                actor="dispatcher",
                run_id=run["run_id"],
                data={
                    "required_output": "session_message",
                    "available_after": self._role_turn_retry_at(),
                },
                created_at=completed_at,
            )
        self._record_role_turn_outcome(
            state,
            run,
            session_id=session["session_id"],
            role=WORKER_AGENT_ROLE,
            protocol_signal=protocol_signal,
        )
        if readiness_entry:
            readiness_entry["status"] = "resolved"
            readiness_entry["resolved_at"] = completed_at
        self._set_role_cursor(
            state,
            session_id=session["session_id"],
            role=WORKER_AGENT_ROLE,
            seq=int(run.get("observed_to_seq") or 0),
        )
        run["completion_state"] = "complete"
        run["lease_state"] = "released"
        run["lease_released_at"] = completed_at
        run.pop("current_phase", None)
        session["updated_at"] = completed_at
        self.save(state)
        return {
            "goal": dict(goal),
            "session": dict(session),
            "unit": dict(lease["unit"]) if lease["unit"] else None,
            "run": dict(run),
            "state_transition": None,
            "message": None,
        }

    def _commit_dispatch_error_locked(self, lease: dict[str, Any], exc: AgentError) -> dict[str, Any]:
        state = self.load()
        run = state["dispatch_runs"].get(lease["run"]["run_id"])
        if run is None:
            return self._recover_missing_lease_run_locked(state, lease)
        goal = state["goals"][lease["goal"]["goal_id"]]
        session = state["sessions"][lease["session"]["session_id"]]
        error_role = str(run.get("role") or GOAL_MANAGER_ROLE)
        fence_reason = self._stale_dispatch_fence_locked(state, run, role=error_role)
        if fence_reason:
            return self._discard_stale_dispatch_result_locked(
                state,
                run,
                lease=lease,
                role=error_role,
                fence_reason=fence_reason,
                result=None,
            )
        readiness_entry = self._readiness_entry_by_id(state, lease["readiness_id"])
        failed_at = utc_now()
        transition = self._set_goal_completion_state(
            state,
            goal,
            "incomplete",
            reason=f"Dispatch failed: {exc}",
            actor="dispatcher",
            run_id=run["run_id"],
        )
        goal["failed_at"] = failed_at
        run["completion_state"] = "incomplete"
        run["lease_state"] = "released"
        run["lease_released_at"] = failed_at
        run.pop("current_phase", None)
        run["failed_at"] = failed_at
        run["error"] = str(exc)
        if readiness_entry:
            readiness_entry["status"] = "failed"
            readiness_entry["failed_at"] = failed_at
        self._set_role_cursor(
            state,
            session_id=session["session_id"],
            role=str(run.get("role") or GOAL_MANAGER_ROLE),
            seq=int(run.get("observed_to_seq") or 0),
        )
        self._mark_session_messages_delivered(state, lease["session_messages"], delivered_at=failed_at)
        self._append_dispatch_step_message(
            state,
            run,
            session_id=session["session_id"],
            sender="dispatcher",
            recipient=str(goal.get("created_by") or ROOT_USERNAME),
            body=f"Dispatch failed: {exc}",
            provider="local",
            phase="DispatchError",
        )
        session["updated_at"] = failed_at
        self.save(state)
        return {
            "goal": dict(goal),
            "session": dict(session),
            "unit": dict(lease["unit"]) if lease["unit"] else None,
            "run": dict(run),
            "state_transition": transition,
            "message": None,
        }

    def _recover_missing_lease_run_locked(self, state: dict[str, Any], lease: dict[str, Any]) -> dict[str, Any]:
        run_id = str(lease["run"]["run_id"])
        session_id = str(lease["session"]["session_id"])
        readiness_entry = self._readiness_entry_by_id(state, lease["readiness_id"])
        recovered_at = utc_now()
        if readiness_entry and readiness_entry.get("status") == "acquired":
            readiness_entry["status"] = "ready"
            readiness_entry["recovered_at"] = recovered_at
            readiness_entry.pop("acquired_at", None)
        if session_id in state.get("sessions", {}):
            self._log_system_signal(
                state,
                session_id,
                signal_type="DispatchRunRecovered",
                body=(
                    f"Dispatch run {run_id} was missing when its Agent result returned. "
                    "The request was returned to the dispatch queue."
                ),
                target_roles=[GOAL_MANAGER_ROLE],
                actor="dispatcher",
                run_id=run_id,
                data={"readiness_id": lease["readiness_id"], "role": lease["role"]},
                created_at=recovered_at,
            )
        self.save(state)
        return {
            "goal": dict(state.get("goals", {}).get(lease["goal"]["goal_id"], lease["goal"])),
            "session": dict(state.get("sessions", {}).get(session_id, lease["session"])),
            "unit": dict(lease["unit"]) if lease["unit"] else None,
            "run": {**dict(lease["run"]), "lease_state": "lost"},
            "state_transition": None,
            "message": None,
        }

    def _session_messages_for_dispatch(self, state: dict[str, Any], *, session_id: str) -> list[dict[str, Any]]:
        return self._session_messages(state, session_id)

    def _mark_session_messages_delivered(
        self,
        state: dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        delivered_at: str,
    ) -> None:
        for message in messages:
            endpoint = str(message.get("to") or "")
            if endpoint.startswith("session:"):
                self._advance_endpoint_cursor(state, endpoint, messages)
                return

    def _messages_for_run_locked(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        run_id: str,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for message in self._session_messages(state, session_id):
            payload = message.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get("run_id") or "") != run_id:
                continue
            messages.append(dict(message))
        return messages

    def _dispatch_messages_for_readiness_locked(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        readiness_entry: dict[str, Any],
    ) -> list[dict[str, Any]]:
        from_seq = readiness_entry.get("from_log_seq")
        observed_to_seq = readiness_entry.get("observed_to_seq")
        messages: list[dict[str, Any]] = []
        if from_seq is None or observed_to_seq is None:
            return messages
        for entry in self._session_log_entries(state, session_id):
            seq = int(entry.get("seq") or 0)
            if seq < int(from_seq) or seq > int(observed_to_seq):
                continue
            message = self._message_for_log_entry(state, entry)
            if message:
                messages.append(dict(message))
        return messages

    def _dispatch_signals_for_readiness_locked(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        role: str,
        readiness_entry: dict[str, Any],
    ) -> list[dict[str, Any]]:
        from_seq = readiness_entry.get("from_log_seq")
        observed_to_seq = readiness_entry.get("observed_to_seq")
        if from_seq is None or observed_to_seq is None:
            return []
        return self._system_signals_for_log_range(
            state,
            session_id=session_id,
            from_seq=int(from_seq),
            to_seq=int(observed_to_seq),
            target_filter=role,
        )

    def _stale_dispatch_fence_locked(self, state: dict[str, Any], run: dict[str, Any], *, role: str) -> str:
        if str(run.get("role") or "") != role:
            return f"dispatch run belongs to another role: {run.get('role')}"
        if str(run.get("lease_state") or "") != "acquired":
            return f"dispatch run is no longer acquired: lease_state={run.get('lease_state')}"
        goal = state.get("goals", {}).get(str(run.get("goal_id") or ""))
        if not goal:
            return "dispatch run references a missing SessionGoal"
        if "observed_goal_body" in run and str(goal.get("body") or "") != str(run.get("observed_goal_body")):
            return "the observed SessionGoal was updated during the dispatch run"
        conflict = self._new_requirement_conflicts_after_window(
            state,
            session_id=str(run.get("session_id") or ""),
            run_id=str(run.get("run_id") or ""),
            observed_to_seq=run.get("observed_to_seq"),
        )
        if conflict:
            return conflict
        return ""

    def _new_requirement_conflicts_after_window(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        run_id: str,
        observed_to_seq: int | None,
    ) -> str:
        if observed_to_seq is None:
            return ""
        observed_to_seq = int(observed_to_seq)
        latest_seq = self._latest_session_log_seq(state, session_id=session_id)
        if latest_seq <= observed_to_seq:
            return ""
        for entry in self._session_log_after_cursor(state, session_id=session_id, cursor=observed_to_seq):
            kind = str(entry.get("kind") or "")
            event = entry.get("event")
            event_run_id = str(event.get("run_id") or "") if isinstance(event, dict) else ""
            if kind == "GoalBodyUpdated":
                if event_run_id == run_id:
                    continue
                return "a SessionGoal body revision arrived after the run's observation window"
            if kind == "Message":
                message = self._message_for_log_entry(state, entry)
                payload = message.get("payload") if isinstance(message, dict) else None
                if isinstance(payload, dict) and str(payload.get("run_id") or "") == run_id:
                    continue
                return "a newer Session message arrived after the run's observation window"
            if kind == "GoalStateChanged":
                if event_run_id == run_id:
                    continue
                return "a newer SessionGoal state change arrived after the run's observation window"
            if kind == "ChildGoalCompleted":
                return "a child SessionGoal completed after the run's observation window"
        return ""

    def _discard_stale_dispatch_result_locked(
        self,
        state: dict[str, Any],
        run: dict[str, Any],
        *,
        lease: dict[str, Any],
        role: str,
        fence_reason: str,
        result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        recorded_at = utc_now()
        agent_result = result.get("agent_result") if isinstance(result, dict) else None
        output_body = str(agent_result.output) if agent_result is not None else ""
        run.setdefault("stale_results", []).append(
            {
                "created_at": recorded_at,
                "role": role,
                "fence_reason": fence_reason,
                "output": output_body,
                "output_chars": len(output_body),
            }
        )
        run["stale_result_at"] = recorded_at
        run["stale_result_fenced"] = True
        if str(run.get("lease_state") or "") == "acquired":
            run["lease_state"] = "released"
            run["lease_released_at"] = recorded_at
            run.pop("current_phase", None)
            readiness_entry = self._readiness_entry_by_id(state, str(run.get("readiness_id") or ""))
            if readiness_entry and readiness_entry.get("status") == "acquired":
                readiness_entry["status"] = "stale"
                readiness_entry["stale_at"] = recorded_at
                readiness_entry["stale_reason"] = f"dispatch result discarded: {fence_reason}"
                readiness_entry.pop("acquired_at", None)
            self._set_role_cursor(
                state,
                session_id=str(run.get("session_id") or ""),
                role=role,
                seq=int(run.get("observed_to_seq") or 0),
            )
        self._log_system_signal(
            state,
            str(run.get("session_id") or ""),
            signal_type="StaleDispatchResultDiscarded",
            body=(
                f"Dispatch run {run.get('run_id')} returned after it lost its dispatch ownership or its observed "
                f"requirement changed: {fence_reason}. The returned output was kept as run evidence without "
                "advancing the SessionGoal."
            ),
            target_roles=[GOAL_MANAGER_ROLE],
            actor="dispatcher",
            run_id=str(run.get("run_id") or ""),
            data={"role": role, "stale_output_chars": len(output_body), "fence_reason": fence_reason},
            created_at=recorded_at,
        )
        self.save(state)
        return {
            "goal": dict(state.get("goals", {}).get(lease["goal"]["goal_id"], lease["goal"])),
            "session": dict(state.get("sessions", {}).get(lease["session"]["session_id"], lease["session"])),
            "unit": dict(lease["unit"]) if lease.get("unit") else None,
            "run": dict(run),
            "state_transition": None,
            "message": None,
        }

    def _append_implicit_worker_request_for_incomplete_goal(
        self,
        state: dict[str, Any],
        *,
        goal: dict[str, Any],
        session: dict[str, Any],
        run: dict[str, Any],
        reason: str,
        created_at: str,
    ) -> dict[str, Any]:
        body = self._implicit_worker_request_body(goal=goal, reason=reason)
        return self._append_session_message_locked(
            state,
            session_id=session["session_id"],
            from_endpoint=agent_sender_endpoint(GOAL_MANAGER_ROLE, str(run.get("agent_user") or "")),
            to_endpoint=normalize_endpoint(SESSION_RECIPIENT, session_id=session["session_id"]),
            payload={
                "body": body,
                "worker_request": True,
                "worker_role": WORKER_AGENT_ROLE,
                "agent_role": GOAL_MANAGER_ROLE,
                "agent_user": str(run.get("agent_user") or ""),
                "run_id": run["run_id"],
                "implicit_worker_request": True,
            },
            created_at=created_at,
        )

    def _implicit_worker_request_body(self, *, goal: dict[str, Any], reason: str) -> str:
        normalized_reason = str(reason or "").strip() or "Work toward the incomplete SessionGoal."
        return "\n\n".join(
            [
                "GoalManager marked the SessionGoal incomplete. Treat this as WorkerAgent work.",
                f"SessionGoal:\n{str(goal.get('body') or '').strip()}",
                f"GoalManager incomplete instruction:\n{normalized_reason}",
                "Work toward the SessionGoal, report progress/results to Session, and do not decide goal completion.",
            ]
        )

    def _has_worker_request_for_run(self, state: dict[str, Any], *, session_id: str, run_id: str) -> bool:
        for message in self._session_messages(state, session_id):
            payload = message.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get("run_id") or "") != run_id:
                continue
            if payload.get("worker_request") is not True:
                continue
            return True
        return False

    def _resolve_goal_manager_readiness_for_goal(
        self,
        state: dict[str, Any],
        goal_id: str,
        *,
        resolved_at: str,
    ) -> None:
        for entry in state.setdefault("dispatch_readiness", []):
            if entry.get("goal_id") != goal_id:
                continue
            if entry.get("role", GOAL_MANAGER_ROLE) != GOAL_MANAGER_ROLE:
                continue
            if entry.get("status") not in {"ready", "acquired"}:
                continue
            entry["status"] = "resolved"
            entry["resolved_at"] = resolved_at

    def _declared_goal_outcome_for_run(
        self,
        state: dict[str, Any],
        *,
        run_id: str,
    ) -> dict[str, Any] | None:
        run = state.setdefault("dispatch_runs", {}).get(run_id)
        if not isinstance(run, dict):
            return None
        declaration = run.get("declared_outcome")
        return dict(declaration) if isinstance(declaration, dict) else None

    def _select_dispatch_account(self, state: dict[str, Any], *, role: str, profile: dict[str, Any]) -> dict[str, Any] | None:
        """Appoint the service identity that acts in this run: the first
        eligible account in the slot's priority list whose provider is
        available; an unavailable provider falls through to the next entry."""
        requested = [str(name).strip() for name in (profile.get("accounts") or []) if str(name).strip()]
        if not requested:
            return None
        slot_group = slot_group_name(role)
        fallback: dict[str, Any] | None = None
        for name in requested:
            user = state.setdefault("users", {}).get(name)
            if not isinstance(user, dict):
                continue
            if str(user.get("status") or "") not in {"active", "service"}:
                continue
            if slot_group and slot_group not in [str(group) for group in user.get("groups") or []]:
                continue
            provider = str(user.get("provider") or profile.get("provider") or DEFAULT_AGENT_PROVIDER)
            if fallback is None:
                fallback = {"username": str(user.get("username")), "provider": provider}
            if provider_is_available(provider):
                return {"username": str(user.get("username")), "provider": provider}
        return fallback

    def _ensure_agent_thread(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        role: str,
        account: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        threads = state["agent_threads"]
        now = utc_now()
        legacy_key = f"{session_id}:{role}"
        account_name = str(account or "").strip()
        if not account_name:
            if legacy_key not in threads:
                threads[legacy_key] = {
                    "thread_id": legacy_key,
                    "session_id": session_id,
                    "role": role,
                    "group": slot_group_name(role),
                    "resume_token": f"thread-{new_id('agent')}",
                    "created_at": now,
                    "updated_at": now,
                    "turns": [],
                }
            return threads[legacy_key]
        thread_key = f"{session_id}:{role}:{account_name}"
        if thread_key in threads:
            return threads[thread_key]
        # A legacy role-keyed thread may be adopted by the auto-registered
        # account of the provider that already owns its provider threads, so
        # continuation context is never lost while identity binding tightens.
        legacy_thread = threads.get(legacy_key)
        if isinstance(legacy_thread, dict) and not str(legacy_thread.get("account") or "").strip():
            provider_threads = legacy_thread.get("provider_threads")
            foreign_providers = [
                str(key)
                for key in (provider_threads or {})
                if str(key) != str(provider or "")
            ]
            if not foreign_providers:
                threads.pop(legacy_key)
                legacy_thread["thread_id"] = thread_key
                legacy_thread["account"] = account_name
                legacy_thread["group"] = slot_group_name(role)
                threads[thread_key] = legacy_thread
                return threads[thread_key]
        threads[thread_key] = {
            "thread_id": thread_key,
            "session_id": session_id,
            "role": role,
            "group": slot_group_name(role),
            "account": account_name,
            "resume_token": f"thread-{new_id('agent')}",
            "created_at": now,
            "updated_at": now,
            "turns": [],
        }
        return threads[thread_key]

    def _record_agent_turn(
        self,
        thread: dict[str, Any],
        *,
        prompt: str,
        result: str,
        provider: str | None = None,
        provider_thread_id: str | None = None,
    ) -> None:
        now = utc_now()
        thread["updated_at"] = now
        thread.setdefault("turns", []).append(
            {
                "created_at": now,
                "prompt": prompt,
                "result": result,
            }
        )
        if provider and provider_thread_id:
            thread.setdefault("provider_threads", {})[provider] = provider_thread_id

    def _agent_runtime_env(
        self,
        *,
        session_id: str,
        role: str,
        run_id: str,
        session: dict[str, Any],
        unit: dict[str, Any] | None,
        agent_user: str = "",
        agent_group: str = "",
    ) -> dict[str, str]:
        workspace_path = self._session_workspace_abs_path(session)
        src_path = str(Path(__file__).resolve().parent)
        existing_pythonpath = os.environ.get("PYTHONPATH", "")
        env = {
            "AIZE_STATE_ROOT": str(self.root),
            "AIZE_SESSION_ID": session_id,
            "AIZE_SESSION_WORKSPACE": str(workspace_path),
            "AIZE_AGENT_ROLE": role,
            "AIZE_AGENT_GROUP": agent_group or slot_group_name(role),
            "AIZE_AGENT_USER": agent_user,
            "AIZE_RUN_ID": run_id,
            "PYTHONPATH": f"{src_path}:{existing_pythonpath}" if existing_pythonpath else src_path,
        }
        if unit is not None:
            env["AIZE_UNIT_WORKSPACE"] = str(self._unit_workspace_abs_path(unit))
        return env

    def _append_dispatch_step_message(
        self,
        state: dict[str, Any],
        run: dict[str, Any],
        *,
        session_id: str,
        sender: str,
        recipient: str,
        body: str,
        provider: str,
        phase: str,
    ) -> dict[str, Any]:
        created_at = utc_now()
        step = {
            "phase": phase,
            "provider": provider,
            "from": normalize_endpoint(sender, session_id=session_id),
            "to": normalize_endpoint(recipient, session_id=session_id),
            "created_at": created_at,
            "output": body,
            "output_chars": len(body),
        }
        run["steps"].append(step)
        return step

    def _role_turn_session_messages(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        run_id: str,
        run: dict[str, Any] | None = None,
        role: str,
    ) -> list[dict[str, Any]]:
        bound_run = run or state.setdefault("dispatch_runs", {}).get(str(run_id)) or {}
        sender = agent_sender_endpoint(role, str(bound_run.get("agent_user") or ""))
        recipient = normalize_endpoint(SESSION_RECIPIENT, session_id=session_id)
        reports: list[dict[str, Any]] = []
        for message in self._session_messages(state, session_id):
            payload = message.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get("run_id") or "") != run_id:
                continue
            if str(message.get("from") or "") != sender or str(message.get("to") or "") != recipient:
                continue
            reports.append(dict(message))
        return reports

    def _record_role_turn_outcome(
        self,
        state: dict[str, Any],
        run: dict[str, Any],
        *,
        session_id: str,
        role: str,
        goal_decision: dict[str, Any] | None = None,
        protocol_signal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_messages = self._role_turn_session_messages(
            state,
            session_id=session_id,
            run_id=str(run.get("run_id") or ""),
            run=run,
            role=role,
        )
        if role == GOAL_MANAGER_ROLE:
            required_output = "goal_completion_state"
            valid = goal_decision is not None
        else:
            required_output = "session_message"
            valid = bool(session_messages)
        outcome = {
            "contract_version": 1,
            "role": role,
            "required_output": required_output,
            "valid": valid,
            "session_message_ids": [message["message_id"] for message in session_messages],
        }
        if goal_decision is not None:
            outcome["goal_decision"] = dict(goal_decision)
        if protocol_signal is not None:
            event = protocol_signal.get("event")
            if isinstance(event, dict):
                outcome["protocol_signal_id"] = str(event.get("signal_id") or "")
            outcome["protocol_signal_log_id"] = str(protocol_signal.get("log_id") or "")
        run["outcome"] = outcome
        return outcome

    def _role_turn_retry_at(self, delay_seconds: int = 30) -> str:
        retry_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        return retry_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _append_remote_aize_handoff_message(
        self,
        state: dict[str, Any],
        run: dict[str, Any],
        *,
        session_id: str,
        goal: dict[str, Any],
        worker_prompt: str,
    ) -> dict[str, Any]:
        payload = {
            "goal_id": goal["goal_id"],
            "session_id": session_id,
            "body": "WorkerAgent work item for remote AIze. All exchange data is carried as message payload.",
            "remote_aize_worker_handoff": {
                "worker_prompt": worker_prompt,
            },
            "provider": "remote-aize",
            "run_id": run["run_id"],
            "dispatch_step": "RemoteAIzeWorkerHandoff",
        }
        message = self._append_session_message_locked(
            state,
            session_id=session_id,
            from_endpoint="dispatcher",
            to_endpoint=node_endpoint("remote-aize"),
            payload=payload,
            created_at=utc_now(),
        )
        run["steps"].append(
            {
                "phase": "RemoteAIzeWorkerHandoff",
                "provider": "remote-aize",
                "message_id": message["message_id"],
                "created_at": message["created_at"],
            }
        )
        return message

    def dispatch(
        self,
        *,
        limit: int | None = None,
        recovery_context: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit < 1:
            raise StoreError("dispatch limit must be positive")
        results: list[dict[str, Any]] = []
        while limit is None or len(results) < limit:
            result = self.dispatch_once(recovery_context=recovery_context)
            if result is None:
                break
            results.append(result)
        return results
