from __future__ import annotations

from typing import Any

from kernel.model import new_id, utc_now


class SessionLogStorageMixin:
    @staticmethod
    def _parent_session_ids(state: dict[str, Any], child_session_id: str) -> list[str]:
        return sorted(
            {
                str(edge.get("parent_session_id") or "")
                for edge in state.setdefault("session_edges", [])
                if str(edge.get("child_session_id") or "") == child_session_id
                and str(edge.get("parent_session_id") or "")
            }
        )

    def _session_log_entries(self, state: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
        session_logs = state.setdefault("session_logs", {})
        full_ids = state.setdefault("_session_log_full_ids", [])
        if session_id not in full_ids:
            pending = session_logs.get(session_id, [])
            persisted = self.storage.read_session_log(session_id)
            by_seq = {
                int(entry.get("seq") or 0): entry
                for entry in [*persisted, *pending]
                if isinstance(entry, dict)
            }
            session_logs[session_id] = [by_seq[seq] for seq in sorted(by_seq)]
            full_ids.append(session_id)
        return session_logs[session_id]

    def _all_session_log_ids(self, state: dict[str, Any]) -> list[str]:
        return sorted(set(self.storage.session_log_ids()) | set(state.setdefault("session_logs", {})))

    @staticmethod
    def _clear_session_log_cache(state: dict[str, Any]) -> None:
        state["session_logs"] = {}
        state.pop("_session_log_full_ids", None)

    def _append_session_log_entry(
        self,
        state: dict[str, Any],
        session_id: str,
        *,
        kind: str,
        created_at: str | None = None,
        message_id: str | None = None,
        event: dict[str, Any] | None = None,
        event_key: str | None = None,
    ) -> dict[str, Any]:
        entries = state.setdefault("session_logs", {}).setdefault(session_id, [])
        if message_id and any(
            entry.get("kind") == "Message" and str(entry.get("message_id") or "") == message_id
            for entry in entries
        ):
            return entries[-1] if entries else {}
        if event_key and any(str(entry.get("event_key") or "") == event_key for entry in entries):
            return entries[-1] if entries else {}
        persisted_seq = self.storage.latest_session_log_seq(session_id)
        next_seq = max(persisted_seq, int(entries[-1].get("seq") or 0) if entries else 0) + 1
        entry = {
            "log_id": new_id("log"),
            "seq": next_seq,
            "session_id": session_id,
            "kind": kind,
            "created_at": created_at or utc_now(),
        }
        if message_id:
            entry["message_id"] = message_id
        if event is not None:
            entry["event"] = event
        if event_key:
            entry["event_key"] = event_key
        entries.append(entry)
        return entry

    def _log_message_for_session(self, state: dict[str, Any], message: dict[str, Any], session_id: str) -> None:
        entry = self._append_session_log_entry(
            state,
            session_id,
            kind="Message",
            created_at=str(message.get("created_at") or utc_now()),
            message_id=str(message.get("message_id") or ""),
        )
        entry["message"] = message

    def _session_log_after_cursor(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        cursor: int,
    ) -> list[dict[str, Any]]:
        if session_id in state.setdefault("_session_log_full_ids", []):
            entries = state.setdefault("session_logs", {}).get(session_id, [])
        else:
            entries = self.storage.read_session_log(session_id, from_seq=cursor + 1)
            entries.extend(
                entry
                for entry in state.setdefault("session_logs", {}).get(session_id, [])
                if int(entry.get("seq") or 0) > cursor
            )
        return [dict(entry) for entry in entries if int(entry.get("seq") or 0) > cursor]

    def _latest_session_log_seq(self, state: dict[str, Any], *, session_id: str) -> int:
        entries = state.setdefault("session_logs", {}).get(session_id, [])
        pending_seq = int(entries[-1].get("seq") or 0) if entries else 0
        return max(self.storage.latest_session_log_seq(session_id), pending_seq)

    def _set_session_log_cursor(self, state: dict[str, Any], *, session_id: str, cursor_key: str, seq: int) -> None:
        session = state.setdefault("sessions", {}).get(session_id)
        if not session:
            return
        session.setdefault("role_cursors", {})[cursor_key] = max(
            int(session["role_cursors"].get(cursor_key) or 0),
            int(seq),
        )

    def _message_for_log_entry(self, state: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any] | None:
        message_id = str(entry.get("message_id") or "")
        if not message_id:
            return None
        embedded = entry.get("message")
        if isinstance(embedded, dict):
            return embedded
        for message in state.setdefault("messages", []):
            if str(message.get("message_id") or "") == message_id:
                return message
        return None

    def _system_signals_for_log_range(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        from_seq: int,
        to_seq: int,
        target_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        if session_id in state.setdefault("_session_log_full_ids", []):
            entries = state.setdefault("session_logs", {}).get(session_id, [])
        else:
            entries = self.storage.read_session_log(session_id, from_seq=from_seq, to_seq=to_seq)
            entries.extend(state.setdefault("session_logs", {}).get(session_id, []))
        for entry in entries:
            seq = int(entry.get("seq") or 0)
            if seq < from_seq or seq > to_seq or entry.get("kind") != "SystemSignal":
                continue
            event = entry.get("event")
            if not isinstance(event, dict):
                continue
            target_keys = event.get("target_roles")
            if target_filter and isinstance(target_keys, list) and target_filter not in target_keys:
                continue
            signal = dict(event)
            signal["log_id"] = entry.get("log_id")
            signal["seq"] = seq
            signals.append(signal)
        return signals
