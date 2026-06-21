from __future__ import annotations

from typing import Any

from agents import AgentError, AgentRunner
from model import new_id, utc_now
from store_defs import (
    DEFAULT_AGENT_PROVIDER,
    DISPATCH_PRIORITY_RETRY,
    DISPATCH_PRIORITY_WORKER_REQUEST,
    GOAL_MANAGER_ROLE,
    ROOT_USERNAME,
    SESSION_RECIPIENT,
    WORKER_AGENT_ROLE,
    StoreError,
    normalize_endpoint,
    node_endpoint,
)


class DispatchMixin:
    def dispatch_once(
        self,
        *,
        session_id: str | None = None,
        recovery_context: str | None = None,
    ) -> dict[str, Any] | None:
        with self._state_lock():
            lease = self._acquire_dispatch_lease_locked(
                session_id=session_id,
                recovery_context=recovery_context,
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
                    run_messages=run_messages,
                    recovery_context=lease.get("recovery_context") or "",
                )
                agent_result = runner.run(
                    str(agent_profile.get("provider") or "local"),
                    role=GOAL_MANAGER_ROLE,
                    prompt=prompt,
                    resume_token=lease["resume_token"],
                    cwd=self._session_workspace_abs_path(session),
                    runtime_env=self._agent_runtime_env(
                        session_id=str(session["session_id"]),
                        role=GOAL_MANAGER_ROLE,
                        run_id=run_id,
                        session=session,
                        unit=unit,
                    ),
                )
                result = {
                    "prompt": prompt,
                    "agent_result": agent_result,
                    "goal_status": self._extract_goal_manager_status(agent_result.output),
                    "goal_reason": self._extract_goal_manager_reason(agent_result.output),
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
                    recovery_context=lease.get("recovery_context") or "",
                )
                agent_result = runner.run(
                    str(agent_profile.get("provider") or "local"),
                    role=WORKER_AGENT_ROLE,
                    prompt=prompt,
                    resume_token=lease["resume_token"],
                    cwd=self._session_workspace_abs_path(session),
                    runtime_env=self._agent_runtime_env(
                        session_id=str(session["session_id"]),
                        role=WORKER_AGENT_ROLE,
                        run_id=run_id,
                        session=session,
                        unit=unit,
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
    ) -> dict[str, Any] | None:
        state = self.load()
        if session_id and session_id not in state["sessions"]:
            raise StoreError(f"unknown session: {session_id}")
        queue_entry = self._next_dispatch_queue_entry(state, session_id=session_id)
        if not queue_entry:
            return None

        goal = state["goals"][str(queue_entry.get("goal_id") or "")]
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
        queue_entry["status"] = "acquired"
        queue_entry["acquired_at"] = now
        role = str(queue_entry.get("role") or GOAL_MANAGER_ROLE)
        if role not in {GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE}:
            raise StoreError(f"unsupported dispatch role: {role}")
        agent_profile = state["agent_profiles"].get(role, {"provider": DEFAULT_AGENT_PROVIDER})
        agent_thread = self._ensure_agent_thread(state, session_id=actual_session_id, role=role)
        session_messages = self._session_messages_for_dispatch(state, session_id=actual_session_id)
        dispatch_messages = self._dispatch_messages_for_queue_locked(
            state,
            session_id=actual_session_id,
            queue_entry=queue_entry,
        )
        run_id = new_id("run")
        run = {
            "run_id": run_id,
            "goal_id": goal["goal_id"],
            "session_id": actual_session_id,
            "role": role,
            "unit_id": unit_id,
            "lease_state": "acquired",
            "queue_id": queue_entry["queue_id"],
            "queue_priority": queue_entry.get("priority"),
            "trigger_message_id": queue_entry.get("trigger_message_id"),
            "created_at": now,
            "lease_acquired_at": now,
            "steps": [],
            "session_message_ids": [message["message_id"] for message in session_messages],
            "current_phase": role,
        }
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
            "queue_id": queue_entry["queue_id"],
            "queue_entry": dict(queue_entry),
            "session_messages": [dict(message) for message in session_messages],
            "dispatch_messages": [dict(message) for message in dispatch_messages],
            "role": role,
            "agent_profile": dict(agent_profile),
            "resume_token": str(agent_thread["resume_token"]),
            "recovery_context": recovery_context or "",
        }

    def _commit_goal_manager_run_locked(self, lease: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        run = state["dispatch_runs"][lease["run"]["run_id"]]
        goal = state["goals"][lease["goal"]["goal_id"]]
        session = state["sessions"][lease["session"]["session_id"]]
        queue_entry = self._queue_entry_by_id(state, lease["queue_id"])
        thread = self._ensure_agent_thread(state, session_id=session["session_id"], role=GOAL_MANAGER_ROLE)
        self._record_agent_turn(thread, prompt=result["prompt"], result=result["agent_result"].output)
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
        worker_requested = self._has_live_worker_queue_for_goal(state, str(goal.get("goal_id") or ""))
        implicit_worker_message = None
        if result["goal_status"] == "completed":
            transition = self._set_goal_completion_state(
                state,
                goal,
                "complete",
                reason=result["goal_reason"],
                actor=GOAL_MANAGER_ROLE,
                run_id=run["run_id"],
            )
            run["completion_state"] = "complete"
        else:
            transition = self._set_goal_completion_state(
                state,
                goal,
                "incomplete",
                reason=result["goal_reason"],
                actor=GOAL_MANAGER_ROLE,
                run_id=run["run_id"],
                priority=DISPATCH_PRIORITY_RETRY,
                enqueue_on_incomplete=False,
            )
            if not worker_requested:
                implicit_worker_message = self._append_implicit_worker_request_for_incomplete_goal(
                    state,
                    goal=goal,
                    session=session,
                    run=run,
                    reason=result["goal_reason"],
                    output=result["agent_result"].output,
                    created_at=completed_at,
                )
                worker_requested = True
            if worker_requested:
                self._resolve_goal_manager_queue_entries_for_goal(
                    state,
                    str(goal.get("goal_id") or ""),
                    resolved_at=completed_at,
                )
            run["completion_state"] = "incomplete"
            run["goal_manager_status"] = result["goal_status"]
        if queue_entry and result["goal_status"] == "completed":
            queue_entry["status"] = "resolved"
            queue_entry["resolved_at"] = completed_at
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

    def _commit_worker_run_locked(self, lease: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        run = state["dispatch_runs"][lease["run"]["run_id"]]
        goal = state["goals"][lease["goal"]["goal_id"]]
        session = state["sessions"][lease["session"]["session_id"]]
        queue_entry = self._queue_entry_by_id(state, lease["queue_id"])
        thread = self._ensure_agent_thread(state, session_id=session["session_id"], role=WORKER_AGENT_ROLE)
        self._record_agent_turn(thread, prompt=result["prompt"], result=result["agent_result"].output)
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
        if queue_entry:
            queue_entry["status"] = "resolved"
            queue_entry["resolved_at"] = completed_at
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
        run = state["dispatch_runs"][lease["run"]["run_id"]]
        goal = state["goals"][lease["goal"]["goal_id"]]
        session = state["sessions"][lease["session"]["session_id"]]
        queue_entry = self._queue_entry_by_id(state, lease["queue_id"])
        failed_at = utc_now()
        transition = self._set_goal_completion_state(
            state,
            goal,
            "incomplete",
            reason=f"Dispatch failed: {exc}",
            actor="dispatcher",
            run_id=run["run_id"],
            priority=DISPATCH_PRIORITY_RETRY,
            enqueue_on_incomplete=False,
        )
        goal["failed_at"] = failed_at
        run["completion_state"] = "incomplete"
        run["lease_state"] = "released"
        run["lease_released_at"] = failed_at
        run.pop("current_phase", None)
        run["failed_at"] = failed_at
        run["error"] = str(exc)
        if queue_entry:
            queue_entry["status"] = "failed"
            queue_entry["failed_at"] = failed_at
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

    def _session_messages_for_dispatch(self, state: dict[str, Any], *, session_id: str) -> list[dict[str, Any]]:
        indexed_ids = {
            str(item.get("message_id") or "")
            for item in state.setdefault("message_index", [])
            if item.get("session_id") == session_id
        }
        return [
            dict(message)
            for message in state.get("messages", [])
            if str(message.get("message_id") or "") in indexed_ids
        ]

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
        for message in state.get("messages", []):
            payload = message.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get("run_id") or "") != run_id:
                continue
            if not any(
                item.get("message_id") == message.get("message_id") and item.get("session_id") == session_id
                for item in state.setdefault("message_index", [])
            ):
                continue
            messages.append(dict(message))
        return messages

    def _dispatch_messages_for_queue_locked(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        queue_entry: dict[str, Any],
    ) -> list[dict[str, Any]]:
        trigger_message_id = str(queue_entry.get("trigger_message_id") or "").strip()
        messages: list[dict[str, Any]] = []
        for message in state.get("messages", []):
            if trigger_message_id:
                if str(message.get("message_id") or "") != trigger_message_id:
                    continue
            else:
                role = str(queue_entry.get("role") or GOAL_MANAGER_ROLE)
                payload = message.get("payload")
                if not isinstance(payload, dict):
                    continue
                if role == WORKER_AGENT_ROLE and payload.get("worker_request") is not True:
                    continue
                if role == GOAL_MANAGER_ROLE and not (
                    payload.get("user_input") is True
                    or message.get("from") == normalize_endpoint(WORKER_AGENT_ROLE, session_id=session_id)
                ):
                    continue
            if not any(
                item.get("message_id") == message.get("message_id") and item.get("session_id") == session_id
                for item in state.setdefault("message_index", [])
            ):
                continue
            messages.append(dict(message))
        return messages

    def _append_implicit_worker_request_for_incomplete_goal(
        self,
        state: dict[str, Any],
        *,
        goal: dict[str, Any],
        session: dict[str, Any],
        run: dict[str, Any],
        reason: str,
        output: str,
        created_at: str,
    ) -> dict[str, Any]:
        body = self._implicit_worker_request_body(goal=goal, reason=reason, output=output)
        message = self._message(
            from_endpoint=normalize_endpoint(GOAL_MANAGER_ROLE, session_id=session["session_id"]),
            to_endpoint=normalize_endpoint(SESSION_RECIPIENT, session_id=session["session_id"]),
            payload={
                "body": body,
                "worker_request": True,
                "worker_role": WORKER_AGENT_ROLE,
                "run_id": run["run_id"],
                "implicit_worker_request": True,
            },
            created_at=created_at,
        )
        state["messages"].append(message)
        self._index_message_for_session(state, message, session["session_id"])
        self._enqueue_dispatch(
            state,
            goal,
            priority=DISPATCH_PRIORITY_WORKER_REQUEST,
            reason=f"GoalManager incomplete result {message['message_id']} requires WorkerAgent work.",
            role=WORKER_AGENT_ROLE,
            trigger_message_id=message["message_id"],
        )
        return message

    def _implicit_worker_request_body(self, *, goal: dict[str, Any], reason: str, output: str) -> str:
        normalized_reason = str(reason or "").strip() or "Work toward the incomplete SessionGoal."
        normalized_output = str(output or "").strip()
        if len(normalized_output) > 4000:
            normalized_output = f"{normalized_output[:4000]}\n...[truncated]"
        return "\n\n".join(
            [
                "GoalManager marked the SessionGoal incomplete. Treat this as WorkerAgent work.",
                f"SessionGoal:\n{str(goal.get('body') or '').strip()}",
                f"GoalManager incomplete instruction:\n{normalized_reason}",
                f"GoalManager output:\n{normalized_output}",
                "Work toward the SessionGoal, report progress/results to Session, and do not decide goal completion.",
            ]
        )

    def _has_live_worker_queue_for_goal(self, state: dict[str, Any], goal_id: str) -> bool:
        return any(
            entry.get("goal_id") == goal_id
            and entry.get("role", GOAL_MANAGER_ROLE) == WORKER_AGENT_ROLE
            and entry.get("status") in {"queued", "acquired"}
            for entry in state.setdefault("dispatch_queue", [])
        )

    def _resolve_goal_manager_queue_entries_for_goal(
        self,
        state: dict[str, Any],
        goal_id: str,
        *,
        resolved_at: str,
    ) -> None:
        for entry in state.setdefault("dispatch_queue", []):
            if entry.get("goal_id") != goal_id:
                continue
            if entry.get("role", GOAL_MANAGER_ROLE) != GOAL_MANAGER_ROLE:
                continue
            if entry.get("status") not in {"queued", "acquired"}:
                continue
            entry["status"] = "resolved"
            entry["resolved_at"] = resolved_at

    def _ensure_agent_thread(self, state: dict[str, Any], *, session_id: str, role: str) -> dict[str, Any]:
        thread_key = f"{session_id}:{role}"
        threads = state["agent_threads"]
        if thread_key not in threads:
            now = utc_now()
            threads[thread_key] = {
                "thread_id": thread_key,
                "session_id": session_id,
                "role": role,
                "resume_token": f"thread-{new_id('agent')}",
                "created_at": now,
                "updated_at": now,
                "turns": [],
            }
        return threads[thread_key]

    def _record_agent_turn(self, thread: dict[str, Any], *, prompt: str, result: str) -> None:
        now = utc_now()
        thread["updated_at"] = now
        thread.setdefault("turns", []).append(
            {
                "created_at": now,
                "prompt": prompt,
                "result": result,
            }
        )

    def _agent_runtime_env(
        self,
        *,
        session_id: str,
        role: str,
        run_id: str,
        session: dict[str, Any],
        unit: dict[str, Any] | None,
    ) -> dict[str, str]:
        workspace_path = self._session_workspace_abs_path(session)
        env = {
            "AIZE_STATE_ROOT": str(self.root),
            "AIZE_SESSION_ID": session_id,
            "AIZE_SESSION_WORKSPACE": str(workspace_path),
            "AIZE_AGENT_ROLE": role,
            "AIZE_RUN_ID": run_id,
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
            "body": "WorkerAgent work item for remote AIZE. All exchange data is carried as message payload.",
            "remote_aize_worker_handoff": {
                "worker_prompt": worker_prompt,
            },
            "provider": "remote-aize",
            "run_id": run["run_id"],
            "dispatch_step": "RemoteAizeWorkerHandoff",
        }
        message = self._message(
            from_endpoint="dispatcher",
            to_endpoint=node_endpoint("remote-aize"),
            payload=payload,
            created_at=utc_now(),
        )
        state["messages"].append(message)
        self._index_message_for_session(state, message, session_id)
        run["steps"].append(
            {
                "phase": "RemoteAizeWorkerHandoff",
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
