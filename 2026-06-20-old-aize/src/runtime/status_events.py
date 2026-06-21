from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from runtime.message_builder import build_outgoing_event_message
from runtime.status_gateway import goal_status_changed_event

HistoryAppender = Callable[[str, str, dict[str, Any]], None]
MessageSender = Callable[[dict[str, Any]], None]


def append_goal_status_changed(
    append_history: HistoryAppender,
    *,
    service_id: str,
    username: str,
    session_id: str,
    session: dict[str, Any] | None,
    previous_session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = goal_status_changed_event(
        service_id=service_id,
        username=username,
        session_id=session_id,
        session=session,
        previous_session=previous_session,
    )
    append_history(username, session_id, entry)
    return entry


def send_status_event_to_router(
    *,
    runtime_root: Path,
    manifest: dict[str, Any],
    from_service_id: str,
    to_service_id: str,
    process_id: str,
    run_id: str,
    entry: dict[str, Any],
    username: str,
    session_id: str,
    send: MessageSender,
) -> None:
    event_message = build_outgoing_event_message(
        runtime_root=runtime_root,
        manifest=manifest,
        from_node_id=str(manifest.get("node_id") or ""),
        from_service_id=from_service_id,
        to_node_id=str(manifest.get("node_id") or ""),
        to_service_id=to_service_id,
        process_id=process_id,
        run_id=run_id,
        entry=entry,
        username=username,
        session_id=session_id,
    )
    send(event_message)


def publish_goal_status_changed(
    append_history: HistoryAppender,
    *,
    service_id: str,
    username: str,
    session_id: str,
    session: dict[str, Any] | None,
    previous_session: dict[str, Any] | None = None,
    runtime_root: Path | None = None,
    manifest: dict[str, Any] | None = None,
    to_service_id: str = "",
    process_id: str = "",
    run_id: str = "",
    send: MessageSender | None = None,
) -> dict[str, Any]:
    entry = append_goal_status_changed(
        append_history,
        service_id=service_id,
        username=username,
        session_id=session_id,
        session=session,
        previous_session=previous_session,
    )
    if runtime_root is not None and manifest is not None and to_service_id and send is not None:
        send_status_event_to_router(
            runtime_root=runtime_root,
            manifest=manifest,
            from_service_id=service_id,
            to_service_id=to_service_id,
            process_id=process_id,
            run_id=run_id,
            entry=entry,
            username=username,
            session_id=session_id,
            send=send,
        )
    return entry
