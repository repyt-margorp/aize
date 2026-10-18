from __future__ import annotations

from typing import Any

from kernel.defs import (
    SESSION_RECIPIENT,
    USER_CONSOLE_RECIPIENT,
    StoreError,
    console_endpoint,
    session_endpoint,
    user_endpoint,
)
from kernel.lock import state_transaction
from kernel.model import utc_now
from goal.defs import (
    AGENT_ROLES,
    GOAL_MANAGER_ROLE,
    ROLE_ALIASES,
    ROLE_MESSAGE_RECIPIENTS,
    SLOT_ROLE_BY_GROUP,
    WORKER_AGENT_ROLE,
    agent_endpoint,
    normalize_agent_role,
    normalize_endpoint,
)


class GoalMessageMixin:
    def _resolve_message_endpoint(self, value: str, *, session_id: str | None = None) -> str:
        return normalize_endpoint(value, session_id=session_id)

    def _resolve_runtime_sender(
        self,
        state: dict[str, Any],
        *,
        sender: str,
        group: str | None = None,
    ) -> tuple[str, str]:
        """Resolve the sending identity of an agent run to its slot role and
        sender endpoint: a service user sends as ``user:{name}``; a legacy role
        token sends as ``agent:{role}``."""
        raw = str(sender or "").strip()
        if not raw:
            raise StoreError("agent runtime sender is required")
        role = normalize_agent_role(raw)
        if role in AGENT_ROLES and (raw in AGENT_ROLES or raw.lower() in ROLE_ALIASES):
            return role, agent_endpoint(role)
        user = state.setdefault("users", {}).get(raw)
        if not isinstance(user, dict):
            raise StoreError(f"agent runtime sender is not allowed: {raw}")
        user_groups = [str(value) for value in user.get("groups") or []]
        claimed_group = str(group or "").strip()
        if claimed_group:
            slot_role = SLOT_ROLE_BY_GROUP.get(claimed_group)
            if not slot_role:
                raise StoreError(f"unknown slot group for agent runtime sender: {claimed_group}")
            if claimed_group not in user_groups:
                raise StoreError(f"agent runtime sender {raw} is not a member of group: {claimed_group}")
        else:
            candidates = [name for name in user_groups if name in SLOT_ROLE_BY_GROUP]
            if len(candidates) != 1:
                raise StoreError(
                    f"agent runtime sender {raw} must claim a slot group: {', '.join(sorted(SLOT_ROLE_BY_GROUP))}"
                )
            slot_role = SLOT_ROLE_BY_GROUP[candidates[0]]
        return slot_role, user_endpoint(raw)

    def _check_runtime_run_binding(self, state: dict[str, Any], *, sender: str, run_id: str | None) -> None:
        if not run_id:
            return
        run = state.setdefault("dispatch_runs", {}).get(str(run_id))
        if not isinstance(run, dict):
            return
        run_user = str(run.get("agent_user") or "").strip()
        if run_user and run_user != str(sender):
            raise StoreError("dispatch run belongs to a different service identity")

    def append_runtime_message(
        self,
        session_id: str,
        *,
        sender: str,
        recipient: str,
        body: str,
        group: str | None = None,
        provider: str | None = None,
        run_id: str | None = None,
        recipient_endpoint_id: str | None = None,
        files: list[dict[str, Any]] | None = None,
        worker_request: bool = False,
        wake_event: str | None = None,
    ) -> dict[str, Any]:
        with self._state_lock():
            state = self.load()
            sessions = state["sessions"]
            if session_id not in sessions:
                raise StoreError(f"unknown session: {session_id}")
            normalized_recipient = str(recipient or "").strip()
            role, from_endpoint = self._resolve_runtime_sender(state, sender=sender, group=group)
            self._check_runtime_run_binding(state, sender=str(sender or "").strip(), run_id=run_id)
            allowed_recipients = ROLE_MESSAGE_RECIPIENTS.get(role, set())
            if normalized_recipient not in allowed_recipients:
                raise StoreError(
                    f"agent runtime recipient is not allowed: {role} -> {normalized_recipient}"
                )
            payload: dict[str, Any] = {"body": body}
            payload["agent_role"] = role
            payload["agent_user"] = "" if from_endpoint.startswith("agent:") else str(sender or "").strip()
            normalized_files = self._normalize_message_files(files)
            if normalized_files:
                payload["files"] = normalized_files
            if worker_request:
                if role != GOAL_MANAGER_ROLE or normalized_recipient != SESSION_RECIPIENT:
                    raise StoreError("worker requests must be GoalManager Messages to Session")
                payload["worker_request"] = True
                payload["worker_role"] = WORKER_AGENT_ROLE
            if wake_event:
                normalized_wake_event = str(wake_event).strip()
                if not normalized_wake_event:
                    raise StoreError("wake_event must be a non-empty name")
                payload["wake_events"] = [normalized_wake_event]
            if provider:
                payload["provider"] = str(provider)
            if run_id:
                payload["run_id"] = str(run_id)
            if normalized_recipient == USER_CONSOLE_RECIPIENT:
                endpoint = str(recipient_endpoint_id or "").strip()
                if not endpoint:
                    endpoint = self._latest_reply_endpoint_for_session(state, session_id=session_id)
                if not endpoint:
                    raise StoreError("UserConsole messages require a reply endpoint")
                to_endpoint = console_endpoint(endpoint)
            else:
                to_endpoint = normalize_endpoint(normalized_recipient, session_id=session_id)
            message = self._append_session_message_locked(
                state,
                session_id=session_id,
                from_endpoint=from_endpoint,
                to_endpoint=to_endpoint,
                payload=payload,
            )
            self.save(state)
            return message

    def append_user_input(
        self,
        session_id: str,
        *,
        sender: str,
        body: str,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        with self._state_lock():
            return self._append_user_input_locked(session_id, sender=sender, body=body, reply_to=reply_to)

    def _append_user_input_locked(
        self,
        session_id: str,
        *,
        sender: str,
        body: str,
        reply_to: str | None,
    ) -> dict[str, Any]:
        state = self.load()
        sessions = state["sessions"]
        if session_id not in sessions:
            raise StoreError(f"unknown session: {session_id}")
        if sender not in state["users"]:
            raise StoreError(f"unknown user: {sender}")
        normalized_reply_to = str(reply_to or "").strip()
        now = utc_now()
        active_worker_run = self._active_worker_run_for_session(state, session_id=session_id)
        payload: dict[str, Any] = {
            "body": body,
            "user_input": True,
        }
        if active_worker_run:
            payload["worker_followup"] = True
            payload["defer_goal_manager_until_worker_report"] = True
        if normalized_reply_to:
            payload["reply_to"] = normalized_reply_to
        message = self._append_session_message_locked(
            state,
            session_id=session_id,
            from_endpoint=user_endpoint(sender),
            to_endpoint=session_endpoint(session_id),
            payload=payload,
            created_at=now,
        )
        target_goal = self._mark_session_reprocess_needed(
            state,
            session_id=session_id,
            reason=f"UserInput message {message['message_id']} requires Session processing.",
            actor=sender,
            created_at=now,
            defer_goal_manager=bool(active_worker_run),
        )
        message["payload"]["reprocess_goal_id"] = target_goal["goal_id"]
        message["payload"]["reprocess_recorded_at"] = now
        if active_worker_run:
            worker_payload = {
                "body": body,
                "user_input": True,
                "forwarded_from": message["message_id"],
                "worker_followup": True,
                "run_id": active_worker_run["run_id"],
                "worker_request": True,
                "worker_role": WORKER_AGENT_ROLE,
            }
            if normalized_reply_to:
                worker_payload["reply_to"] = normalized_reply_to
            self._append_session_message_locked(
                state,
                session_id=session_id,
                from_endpoint=user_endpoint(sender),
                to_endpoint=session_endpoint(session_id),
                payload=worker_payload,
                created_at=now,
            )
        self.save(state)
        return message

    def _active_worker_run_for_session(self, state: dict[str, Any], *, session_id: str) -> dict[str, Any] | None:
        active_runs = [
            run
            for run in state.get("dispatch_runs", {}).values()
            if str(run.get("session_id") or "") == session_id
            and run.get("lease_state") == "acquired"
            and run.get("current_phase") == WORKER_AGENT_ROLE
        ]
        if not active_runs:
            return None
        active_runs.sort(key=lambda item: str(item.get("lease_acquired_at") or item.get("created_at") or ""))
        return dict(active_runs[-1])

    @state_transaction
    def receive_message(self, recipient: str, *, session_id: str | None = None) -> dict[str, Any] | None:
        state = self.load()
        normalized_recipient = str(recipient or "").strip()
        if not normalized_recipient:
            raise StoreError("recipient is required")
        endpoint = normalize_endpoint(normalized_recipient, session_id=session_id)
        messages = self._messages_after_cursor(state, endpoint)
        for message in messages:
            if session_id and str(message.get("to") or "") != session_endpoint(session_id):
                continue
            self._advance_endpoint_cursor(state, endpoint, [message])
            self.save(state)
            return dict(message)
        return None
