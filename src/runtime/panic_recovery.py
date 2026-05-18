from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from kernel.auth import GOAL_MANAGER_USERNAME
from runtime.persistent_state_pkg import (
    add_session_child,
    create_conversation_session,
    get_session_settings,
    list_session_children,
    read_json_file,
    session_operation_allowed,
    session_goal_manager_dir,
    session_metadata_path,
    supersede_recovery_child_sessions,
    update_session_goal,
    update_session_goal_flags,
    write_json_file,
)
from wire.protocol import utc_ts


def _provider_from_service_id(service_id: str) -> str:
    normalized = str(service_id or "").strip().lower()
    if normalized.startswith("service-"):
        parts = normalized.split("-")
        if len(parts) >= 3 and parts[1] in {"codex", "claude", "gemini"}:
            return parts[1]
    return ""


def _transport_like_panic(event: dict[str, Any] | None) -> bool:
    event = dict(event or {})
    text = " ".join(
        str(event.get(key) or "").strip().lower()
        for key in ("reason", "error", "text", "message", "compaction", "wait_status")
    )
    if not text:
        return False
    markers = (
        "transport channel closed",
        "worker quit with fatal",
        "http/request failed",
        "error sending request for url",
        "remote compaction failed",
        "failed to run pre-sampling compact",
        "stream disconnected before completion",
        "unexpected status 403",
        "broken pipe",
        "timeout waiting for child process to exit",
        "reconnecting...",
    )
    return any(marker in text for marker in markers)


def _select_recovery_provider(
    *,
    panic_service_id: str,
    event: dict[str, Any] | None,
    preferred_provider: str,
) -> str:
    normalized_preferred = str(preferred_provider or "").strip().lower()
    panic_provider = _provider_from_service_id(panic_service_id)
    if normalized_preferred not in {"codex", "claude", "gemini"}:
        normalized_preferred = panic_provider or "codex"
    if panic_provider and _transport_like_panic(event):
        for candidate in ("claude", "gemini", "codex"):
            if candidate != panic_provider:
                return candidate
    return normalized_preferred or panic_provider or "codex"


def _recovery_goal_in_progress(session: dict[str, Any] | None) -> bool:
    if not isinstance(session, dict):
        return False
    if not bool(session.get("goal_active", False)):
        return False
    progress_state = str(
        session.get(
            "goal_progress_state",
            "complete" if bool(session.get("goal_completed", False)) else "in_progress",
        )
    ).strip().lower()
    return progress_state == "in_progress"


def _find_active_recovery_session(
    runtime_root: Path,
    *,
    username: str,
    source_session_id: str,
    panic_service_id: str,
) -> dict[str, Any] | None:
    for child_session_id in list_session_children(
        runtime_root,
        username=username,
        session_id=source_session_id,
    ):
        child = get_session_settings(runtime_root, username=username, session_id=child_session_id)
        if not isinstance(child, dict):
            continue
        if str(child.get("session_group") or "").strip().lower() != "error":
            continue
        child_source = str(
            child.get("recovery_source_session_id")
            or child.get("source_session_id")
            or child.get("parent_session_id")
            or ""
        ).strip()
        child_panic_service_id = str(child.get("recovery_panic_service_id") or "").strip()
        if child_source == source_session_id and child_panic_service_id == panic_service_id and _recovery_goal_in_progress(child):
            return child
    return None


def panic_recovery_goal_text(
    *,
    source_session_id: str,
    source_label: str,
    panic_service_id: str,
    event: dict[str, Any] | None,
) -> str:
    event = dict(event or {})
    event_type = str(event.get("type") or "panic").strip() or "panic"
    reason = str(event.get("reason") or event.get("error") or event.get("text") or "").strip()
    compact = str(event.get("compaction") or "").strip()
    wait_status = str(event.get("wait_status") or "").strip()
    left_percent = str(event.get("left_percent") or event.get("post_left_percent") or "").strip()
    lines = [
        "ユーザー指示: Panic に陥った親セッションを復旧させよ。",
        f"対象セッションID: {source_session_id}",
        f"対象セッション名: {source_label or source_session_id}",
        f"Panic を起こしたエージェント: {panic_service_id or 'unknown'}",
        f"失敗イベント: {event_type}",
    ]
    if reason:
        lines.append(f"失敗理由: {reason}")
    if compact:
        lines.append(f"compact 状態: {compact}")
    if wait_status:
        lines.append(f"wait 状態: {wait_status}")
    if left_percent:
        lines.append(f"context left_percent: {left_percent}")
    lines.extend(
        [
            "要件:",
            "1. 親セッションが Panic になった直接原因を特定すること。",
            "2. 最小限の修正で、親セッションが再開できる状態に戻すことを最優先にすること。",
            "3. 無関係な全面リファクタや広範囲修正は行わず、親セッションの続行を妨げる箇所だけ直すこと。",
            "4. 親セッションへ戻すために必要な具体的手順または自動処理まで実装すること。",
            "5. 修正後、親セッションが実際に再開して新しい turn / 応答を出すところまで確認すること。",
            "6. 進捗と結果はこの recovery セッションに報告すること。",
        ]
    )
    return "\n".join(lines)


def panic_recovery_bootstrap_xml(
    *,
    source_session_id: str,
    source_label: str,
    panic_service_id: str,
    event: dict[str, Any] | None,
) -> str:
    event_text = html.escape(json.dumps(event or {}, ensure_ascii=False))
    return (
        "<aize_panic_recovery>"
        f"<source_session_id>{html.escape(source_session_id)}</source_session_id>"
        f"<source_session_label>{html.escape(source_label)}</source_session_label>"
        f"<panic_service_id>{html.escape(panic_service_id)}</panic_service_id>"
        "<instruction>Inspect the immediate panic cause, apply the smallest viable fix, make the source session resumable, and verify that the source session actually resumes with a new turn or reply. Do not broaden scope into a full system rewrite unless that is strictly required to resume the source session.</instruction>"
        f"<panic_event_json>{event_text}</panic_event_json>"
        "</aize_panic_recovery>"
    )


def ensure_panic_recovery_session(
    runtime_root: Path,
    *,
    username: str,
    source_session_id: str,
    source_label: str,
    panic_service_id: str,
    event: dict[str, Any] | None,
    preferred_provider: str,
) -> dict[str, Any] | None:
    source_session = get_session_settings(runtime_root, username=username, session_id=source_session_id)
    if not isinstance(source_session, dict):
        return None
    if not session_operation_allowed(source_session, "auto_spawn_recovery"):
        return None
    recovery_dir = session_goal_manager_dir(
        runtime_root,
        username=username,
        session_id=source_session_id,
    )
    recovery_record_path = recovery_dir / f"panic_recovery.{panic_service_id or 'unknown'}.json"
    event = dict(event or {})
    selected_provider = _select_recovery_provider(
        panic_service_id=panic_service_id,
        event=event,
        preferred_provider=preferred_provider,
    )
    signature = {
        "event_type": str(event.get("type") or ""),
        "reason": str(event.get("reason") or event.get("error") or ""),
        "compaction": str(event.get("compaction") or ""),
        "wait_status": str(event.get("wait_status") or ""),
        "returncode": str(event.get("returncode") or ""),
    }
    existing = read_json_file(recovery_record_path) or {}
    existing_session_id = str(existing.get("recovery_session_id") or "").strip()
    existing_signature = existing.get("panic_signature")
    active_recovery_session = _find_active_recovery_session(
        runtime_root,
        username=username,
        source_session_id=source_session_id,
        panic_service_id=panic_service_id,
    )
    if isinstance(active_recovery_session, dict):
        existing_session_id = str(active_recovery_session.get("session_id") or "").strip()
        existing_signature = signature
    if existing_session_id and existing_signature == signature:
        existing_session = get_session_settings(
            runtime_root,
            username=username,
            session_id=existing_session_id,
        )
        if isinstance(existing_session, dict):
            goal_text = panic_recovery_goal_text(
                source_session_id=source_session_id,
                source_label=source_label,
                panic_service_id=panic_service_id,
                event=event,
            )
            update_session_goal(
                runtime_root,
                username=username,
                session_id=existing_session_id,
                goal_text=goal_text,
                updated_by_username=GOAL_MANAGER_USERNAME,
                updated_by_type="system",
                origin_session_id=source_session_id,
                origin_goal_id=str(source_session.get("active_goal_id") or source_session.get("goal_id") or "").strip(),
                origin_goal_text=str(source_session.get("goal_text") or ""),
            )
            update_session_goal_flags(
                runtime_root,
                username=username,
                session_id=existing_session_id,
                goal_active=True,
                goal_completed=False,
                goal_progress_state="in_progress",
                preferred_provider=selected_provider,
            )
            write_json_file(
                recovery_record_path,
                {
                    "recovery_session_id": existing_session_id,
                    "source_session_id": source_session_id,
                    "panic_service_id": panic_service_id,
                    "panic_signature": signature,
                    "updated_at": utc_ts(),
                    "reused_active_recovery": True,
                },
            )
            supersede_recovery_child_sessions(
                runtime_root,
                username=username,
                parent_session_id=source_session_id,
                keep_session_id=existing_session_id,
            )
            return get_session_settings(runtime_root, username=username, session_id=existing_session_id)
    goal_text = panic_recovery_goal_text(
        source_session_id=source_session_id,
        source_label=source_label,
        panic_service_id=panic_service_id,
        event=event,
    )
    child = create_conversation_session(
        runtime_root,
        username=username,
        label=f"Recovery: {source_label or source_session_id}",
        session_group="error",
        session_ui_mode="standard",
        session_permissions={
            "create_child_session": False,
            "auto_spawn_recovery": False,
        },
        created_by_username=GOAL_MANAGER_USERNAME,
        created_by_type="system",
        origin_session_id=source_session_id,
        origin_goal_id=str(source_session.get("active_goal_id") or source_session.get("goal_id") or "").strip(),
        origin_goal_text=str(source_session.get("goal_text") or ""),
    )
    if not isinstance(child, dict):
        return None
    recovery_session_id = str(child.get("session_id") or "").strip()
    if not recovery_session_id:
        return None
    recovery_session = get_session_settings(
        runtime_root,
        username=username,
        session_id=recovery_session_id,
    ) or {}
    add_session_child(
        runtime_root,
        username=username,
        parent_session_id=source_session_id,
        child_session_id=recovery_session_id,
    )
    recovery_session = get_session_settings(
        runtime_root,
        username=username,
        session_id=recovery_session_id,
    ) or recovery_session
    recovery_session["recovery_source_session_id"] = source_session_id
    recovery_session["recovery_source_label"] = source_label
    recovery_session["recovery_panic_service_id"] = panic_service_id
    recovery_session["updated_at"] = utc_ts()
    write_json_file(
        session_metadata_path(runtime_root, username=username, session_id=recovery_session_id),
        recovery_session,
    )
    update_session_goal(
        runtime_root,
        username=username,
        session_id=recovery_session_id,
        goal_text=goal_text,
        updated_by_username=GOAL_MANAGER_USERNAME,
        updated_by_type="system",
        origin_session_id=source_session_id,
        origin_goal_id=str(source_session.get("active_goal_id") or source_session.get("goal_id") or "").strip(),
        origin_goal_text=str(source_session.get("goal_text") or ""),
    )
    update_session_goal_flags(
        runtime_root,
        username=username,
        session_id=recovery_session_id,
        goal_active=True,
        goal_completed=False,
        goal_progress_state="in_progress",
        preferred_provider=selected_provider,
    )
    write_json_file(
        recovery_record_path,
        {
            "recovery_session_id": recovery_session_id,
            "source_session_id": source_session_id,
            "panic_service_id": panic_service_id,
            "panic_signature": signature,
            "updated_at": utc_ts(),
        },
    )
    supersede_recovery_child_sessions(
        runtime_root,
        username=username,
        parent_session_id=source_session_id,
        keep_session_id=recovery_session_id,
    )
    return get_session_settings(runtime_root, username=username, session_id=recovery_session_id)
