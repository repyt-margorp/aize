from __future__ import annotations

from pathlib import Path
from typing import Any

from ._core import normalize_username, read_json_file, sessions_dir, session_metadata_path, session_service_state_path, state_lock, write_json_file


def load_claude_session(
    runtime_root: Path,
    *,
    service_id: str,
    username: str | None = None,
    session_id: str | None = None,
) -> str | None:
    with state_lock(runtime_root):
        if username and session_id:
            service_state = read_json_file(
                session_service_state_path(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    service_id=service_id,
                )
            )
            if isinstance(service_state, dict):
                provider_session_id = service_state.get("claude_session_id")
                if isinstance(provider_session_id, str) and provider_session_id:
                    return provider_session_id
        return None


def save_claude_session(
    runtime_root: Path,
    *,
    service_id: str,
    provider_session_id: str | None,
    username: str | None = None,
    session_id: str | None = None,
) -> None:
    with state_lock(runtime_root):
        if username and session_id:
            service_state_path = session_service_state_path(
                runtime_root,
                username=username,
                session_id=session_id,
                service_id=service_id,
            )
            service_state = read_json_file(service_state_path) or {"service_id": service_id}
            if provider_session_id:
                service_state["claude_session_id"] = provider_session_id
            else:
                service_state.pop("claude_session_id", None)
            write_json_file(service_state_path, service_state)


def load_codex_session(
    runtime_root: Path,
    *,
    service_id: str,
    username: str | None = None,
    session_id: str | None = None,
) -> str | None:
    with state_lock(runtime_root):
        if username and session_id:
            service_state = read_json_file(
                session_service_state_path(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    service_id=service_id,
                )
            )
            if isinstance(service_state, dict):
                provider_session_id = service_state.get("codex_session_id")
                if isinstance(provider_session_id, str) and provider_session_id:
                    return provider_session_id
        return None


def list_codex_sessions(runtime_root: Path, *, service_id: str) -> list[dict[str, str | None]]:
    with state_lock(runtime_root):
        sessions: list[dict[str, str | None]] = []
        seen: set[tuple[str | None, str | None, str | None]] = set()
        sessions_root = sessions_dir(runtime_root)
        if sessions_root.exists():
            for user_dir in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
                username = normalize_username(user_dir.name)
                for talk_dir in sorted(path for path in user_dir.iterdir() if path.is_dir()):
                    session_metadata = read_json_file(
                        session_metadata_path(runtime_root, username=username, session_id=talk_dir.name)
                    )
                    if not isinstance(session_metadata, dict):
                        continue
                    if str(session_metadata.get("_runtime_root") or "") != str(runtime_root):
                        continue
                    service_state = read_json_file(
                        session_service_state_path(
                            runtime_root,
                            username=username,
                            session_id=talk_dir.name,
                            service_id=service_id,
                        )
                    )
                    if not isinstance(service_state, dict):
                        continue
                    provider_session_id = service_state.get("codex_session_id")
                    if not isinstance(provider_session_id, str) or not provider_session_id:
                        continue
                    item = {
                        "username": username,
                        "conversation_session_id": talk_dir.name,
                        "session_id": provider_session_id,
                    }
                    key = (item["username"], item["conversation_session_id"], item["session_id"])
                    if key not in seen:
                        seen.add(key)
                        sessions.append(item)
        return sessions


def list_claude_sessions(runtime_root: Path, *, service_id: str) -> list[dict[str, str | None]]:
    with state_lock(runtime_root):
        sessions: list[dict[str, str | None]] = []
        seen: set[tuple[str | None, str | None, str | None]] = set()
        sessions_root = sessions_dir(runtime_root)
        if sessions_root.exists():
            for user_dir in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
                username = normalize_username(user_dir.name)
                for talk_dir in sorted(path for path in user_dir.iterdir() if path.is_dir()):
                    session_metadata = read_json_file(
                        session_metadata_path(runtime_root, username=username, session_id=talk_dir.name)
                    )
                    if not isinstance(session_metadata, dict):
                        continue
                    if str(session_metadata.get("_runtime_root") or "") != str(runtime_root):
                        continue
                    service_state = read_json_file(
                        session_service_state_path(
                            runtime_root,
                            username=username,
                            session_id=talk_dir.name,
                            service_id=service_id,
                        )
                    )
                    if not isinstance(service_state, dict):
                        continue
                    provider_session_id = service_state.get("claude_session_id")
                    if not isinstance(provider_session_id, str) or not provider_session_id:
                        continue
                    item = {
                        "username": username,
                        "conversation_session_id": talk_dir.name,
                        "session_id": provider_session_id,
                    }
                    key = (item["username"], item["conversation_session_id"], item["session_id"])
                    if key not in seen:
                        seen.add(key)
                        sessions.append(item)
        return sessions


def save_codex_session(
    runtime_root: Path,
    *,
    service_id: str,
    provider_session_id: str | None,
    username: str | None = None,
    session_id: str | None = None,
) -> None:
    with state_lock(runtime_root):
        if username and session_id:
            service_state_path = session_service_state_path(
                runtime_root,
                username=username,
                session_id=session_id,
                service_id=service_id,
            )
            service_state = read_json_file(service_state_path) or {"service_id": service_id}
            if provider_session_id:
                service_state["codex_session_id"] = provider_session_id
            else:
                service_state.pop("codex_session_id", None)
            write_json_file(service_state_path, service_state)
