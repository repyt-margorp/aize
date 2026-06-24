from __future__ import annotations

from typing import Any

from model import Message, new_id, utc_now
from store_defs import (
    AGENT_ROLES,
    DISPATCH_PRIORITY_USER_INPUT,
    GOAL_MANAGER_ROLE,
    ROLE_MESSAGE_RECIPIENTS,
    SESSION_RECIPIENT,
    USER_CONSOLE_RECIPIENT,
    WORKER_AGENT_ROLE,
    StoreError,
    account_endpoint,
    console_endpoint,
    normalize_endpoint,
    session_endpoint,
)


class MessageMixin:
    def _message(self, *, from_endpoint: str, to_endpoint: str, payload: dict[str, Any], created_at: str | None = None) -> dict[str, Any]:
        return Message(
            message_id=new_id("msg"),
            from_endpoint=from_endpoint,
            to_endpoint=to_endpoint,
            payload=payload,
            created_at=created_at or utc_now(),
        ).to_dict()

    def _index_message_for_session(self, state: dict[str, Any], message: dict[str, Any], session_id: str) -> bool:
        if not session_id:
            return False
        message_id = str(message.get("message_id") or "")
        index = state.setdefault("message_index", [])
        if any(item.get("message_id") == message_id and item.get("session_id") == session_id for item in index):
            return False
        index.append(
            {
                "message_id": message_id,
                "session_id": session_id,
                "created_at": str(message.get("created_at") or utc_now()),
            }
        )
        self._log_message_for_session(state, message, session_id)
        return True

    def _messages_after_cursor(self, state: dict[str, Any], endpoint: str) -> list[dict[str, Any]]:
        messages = state.get("messages", [])
        cursor_id = str(state.setdefault("endpoint_cursors", {}).get(endpoint) or "")
        start_index = -1
        if cursor_id:
            for index, message in enumerate(messages):
                if str(message.get("message_id") or "") == cursor_id:
                    start_index = index
                    break
        return [
            dict(message)
            for message in messages[start_index + 1 :]
            if str(message.get("to") or "") == endpoint
        ]

    def _advance_endpoint_cursor(self, state: dict[str, Any], endpoint: str, messages: list[dict[str, Any]]) -> None:
        for message in reversed(messages):
            if str(message.get("to") or "") == endpoint:
                state.setdefault("endpoint_cursors", {})[endpoint] = str(message.get("message_id") or "")
                return

    def _latest_reply_endpoint_for_session(self, state: dict[str, Any], *, session_id: str) -> str:
        for message in reversed(state.get("messages", [])):
            if str(message.get("to") or "") != session_endpoint(session_id):
                continue
            payload = message.get("payload")
            if not isinstance(payload, dict) or not payload.get("user_input"):
                continue
            reply_to = str(payload.get("reply_to") or "").strip()
            if reply_to:
                return reply_to
        return ""

    def append_message(
        self,
        session_id: str,
        *,
        sender: str,
        recipient: str,
        body: str,
        files: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        sessions = state["sessions"]
        if session_id not in sessions:
            raise StoreError(f"unknown session: {session_id}")
        normalized_files = self._normalize_message_files(files)
        now = utc_now()
        payload: dict[str, Any] = {"body": body}
        if normalized_files:
            payload["files"] = normalized_files
        message = self._message(
            from_endpoint=normalize_endpoint(sender, session_id=session_id),
            to_endpoint=normalize_endpoint(recipient, session_id=session_id),
            payload=payload,
            created_at=now,
        )
        state["messages"].append(message)
        self._index_message_for_session(state, message, session_id)
        sessions[session_id]["updated_at"] = now
        self.save(state)
        return message

    def append_file_message(
        self,
        session_id: str,
        *,
        sender: str,
        recipient: str,
        body: str,
        file_name: str,
        content: str,
        content_type: str = "text/plain",
    ) -> dict[str, Any]:
        payload = {
            "file_name": file_name,
            "content_type": content_type,
            "content": content,
        }
        return self.append_message(
            session_id,
            sender=sender,
            recipient=recipient,
            body=body,
            files=[payload],
        )

    def append_runtime_message(
        self,
        session_id: str,
        *,
        sender: str,
        recipient: str,
        body: str,
        provider: str | None = None,
        run_id: str | None = None,
        recipient_endpoint_id: str | None = None,
        files: list[dict[str, Any]] | None = None,
        worker_request: bool = False,
    ) -> dict[str, Any]:
        with self._state_lock():
            state = self.load()
            sessions = state["sessions"]
            if session_id not in sessions:
                raise StoreError(f"unknown session: {session_id}")
            normalized_sender = str(sender or "").strip()
            normalized_recipient = str(recipient or "").strip()
            if normalized_sender not in AGENT_ROLES:
                raise StoreError(f"agent runtime sender is not allowed: {normalized_sender}")
            allowed_recipients = ROLE_MESSAGE_RECIPIENTS.get(normalized_sender, set())
            if normalized_recipient not in allowed_recipients:
                raise StoreError(
                    f"agent runtime recipient is not allowed: {normalized_sender} -> {normalized_recipient}"
                )
            now = utc_now()
            payload: dict[str, Any] = {"body": body}
            normalized_files = self._normalize_message_files(files)
            if normalized_files:
                payload["files"] = normalized_files
            if worker_request:
                if normalized_sender != GOAL_MANAGER_ROLE or normalized_recipient != SESSION_RECIPIENT:
                    raise StoreError("worker requests must be GoalManager Messages to Session")
                payload["worker_request"] = True
                payload["worker_role"] = WORKER_AGENT_ROLE
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
            message = self._message(
                from_endpoint=normalize_endpoint(normalized_sender, session_id=session_id),
                to_endpoint=to_endpoint,
                payload=payload,
                created_at=now,
            )
            state["messages"].append(message)
            self._index_message_for_session(state, message, session_id)
            sessions[session_id]["updated_at"] = now
            self.save(state)
            return message

    def _normalize_message_files(self, files: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not files:
            return None
        normalized_files: list[dict[str, Any]] = []
        for file_payload in files:
            if not isinstance(file_payload, dict):
                raise StoreError("message file payload must be an object")
            normalized_files.append(dict(file_payload))
        return normalized_files

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
        if sender not in state["accounts"]:
            raise StoreError(f"unknown account: {sender}")
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
        message = self._message(
            from_endpoint=account_endpoint(sender),
            to_endpoint=session_endpoint(session_id),
            payload=payload,
            created_at=now,
        )
        state["messages"].append(message)
        self._index_message_for_session(state, message, session_id)
        target_goal = self._mark_session_reprocess_needed(
            state,
            session_id=session_id,
            reason=f"UserInput message {message['message_id']} requires Session processing.",
            actor=sender,
            priority=DISPATCH_PRIORITY_USER_INPUT,
            created_at=now,
            trigger_message_id=message["message_id"],
            enqueue_on_incomplete=False,
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
            worker_message = self._message(
                from_endpoint=account_endpoint(sender),
                to_endpoint=session_endpoint(session_id),
                payload=worker_payload,
                created_at=now,
            )
            state["messages"].append(worker_message)
            self._index_message_for_session(state, worker_message, session_id)
        sessions[session_id]["updated_at"] = now
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
