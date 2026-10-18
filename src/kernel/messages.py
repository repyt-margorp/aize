from __future__ import annotations

from typing import Any

from kernel.model import Message, new_id, utc_now
from kernel.defs import (
    StoreError,
    canonical_endpoint,
    legacy_endpoint_alias,
    normalize_endpoint,
    session_endpoint,
)


class MessageCoreMixin:
    def _resolve_message_endpoint(self, value: str, *, session_id: str | None = None) -> str:
        return normalize_endpoint(value, session_id=session_id)

    def _message(self, *, from_endpoint: str, to_endpoint: str, payload: dict[str, Any], created_at: str | None = None) -> dict[str, Any]:
        return Message(
            message_id=new_id("msg"),
            from_endpoint=from_endpoint,
            to_endpoint=to_endpoint,
            payload=payload,
            created_at=created_at or utc_now(),
        ).to_dict()

    def _append_session_message_locked(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        from_endpoint: str,
        to_endpoint: str,
        payload: dict[str, Any],
        created_at: str | None = None,
    ) -> dict[str, Any]:
        if session_id not in state.setdefault("sessions", {}):
            raise StoreError(f"unknown session: {session_id}")
        message = self._message(
            from_endpoint=from_endpoint,
            to_endpoint=to_endpoint,
            payload=dict(payload),
            created_at=created_at,
        )
        self._log_message_for_session(state, message, session_id)
        state["sessions"][session_id]["updated_at"] = str(message["created_at"])
        return message

    def _session_messages(self, state: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for entry in self._session_log_entries(state, session_id):
            if entry.get("kind") != "Message":
                continue
            message = self._message_for_log_entry(state, entry)
            if message:
                messages.append(dict(message))
        return messages

    def _all_messages(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        indexed_messages: list[tuple[str, str, int, dict[str, Any]]] = []
        seen: set[str] = set()
        for session_id in self._all_session_log_ids(state):
            for entry in self._session_log_entries(state, session_id):
                if entry.get("kind") != "Message":
                    continue
                message = self._message_for_log_entry(state, entry)
                if not message:
                    continue
                message_id = str(message.get("message_id") or "")
                if message_id in seen:
                    continue
                seen.add(message_id)
                indexed_messages.append(
                    (
                        str(message.get("created_at") or ""),
                        session_id,
                        int(entry.get("seq") or 0),
                        dict(message),
                    )
                )
        return [item[3] for item in sorted(indexed_messages, key=lambda item: item[:3])]

    def _messages_after_cursor(self, state: dict[str, Any], endpoint: str) -> list[dict[str, Any]]:
        messages = self._all_messages(state)
        endpoint = canonical_endpoint(endpoint)
        cursors = state.setdefault("endpoint_cursors", {})
        cursor_id = str(cursors.get(endpoint) or cursors.get(legacy_endpoint_alias(endpoint)) or "")
        start_index = -1
        if cursor_id:
            for index, message in enumerate(messages):
                if str(message.get("message_id") or "") == cursor_id:
                    start_index = index
                    break
        return [
            dict(message)
            for message in messages[start_index + 1 :]
            if canonical_endpoint(str(message.get("to") or "")) == endpoint
        ]

    def _advance_endpoint_cursor(self, state: dict[str, Any], endpoint: str, messages: list[dict[str, Any]]) -> None:
        endpoint = canonical_endpoint(endpoint)
        for message in reversed(messages):
            if canonical_endpoint(str(message.get("to") or "")) == endpoint:
                state.setdefault("endpoint_cursors", {})[endpoint] = str(message.get("message_id") or "")
                return

    def _latest_reply_endpoint_for_session(self, state: dict[str, Any], *, session_id: str) -> str:
        for message in reversed(self._session_messages(state, session_id)):
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
        with self._state_lock():
            state = self.load()
            if session_id not in state["sessions"]:
                raise StoreError(f"unknown session: {session_id}")
            normalized_files = self._normalize_message_files(files)
            payload: dict[str, Any] = {"body": body}
            if normalized_files:
                payload["files"] = normalized_files
            from_endpoint = self._resolve_message_endpoint(sender, session_id=session_id)
            to_endpoint = self._resolve_message_endpoint(recipient, session_id=session_id)
            if from_endpoint.startswith("agent:") or to_endpoint.startswith("agent:"):
                raise StoreError("generic messages may not use agent endpoints")
            for endpoint in (from_endpoint, to_endpoint):
                if endpoint.startswith("user:") and self._is_service_identity(state, endpoint.split(":", 1)[1]):
                    raise StoreError("generic messages may not use service identities as endpoints")
            message = self._append_session_message_locked(
                state,
                session_id=session_id,
                from_endpoint=from_endpoint,
                to_endpoint=to_endpoint,
                payload=payload,
            )
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

    def _normalize_message_files(self, files: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not files:
            return None
        normalized_files: list[dict[str, Any]] = []
        for file_payload in files:
            if not isinstance(file_payload, dict):
                raise StoreError("message file payload must be an object")
            normalized_files.append(dict(file_payload))
        return normalized_files

    def _is_service_identity(self, state: dict[str, Any], username: str) -> bool:
        user = state.setdefault("users", {}).get(str(username))
        return isinstance(user, dict) and str(user.get("status") or "") == "service"

