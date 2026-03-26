from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from kernel.auth import GOAL_MANAGER_USERNAME
from runtime.persistent_state import (
    add_session_child,
    create_conversation_session,
    get_session_settings,
    read_json_file,
    session_operation_allowed,
    session_goal_manager_dir,
    session_metadata_path,
    update_session_goal,
    update_session_goal_flags,
    write_json_file,
)
from wire.protocol import utc_ts


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
                preferred_provider=preferred_provider,
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
        preferred_provider=preferred_provider,
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
    return get_session_settings(runtime_root, username=username, session_id=recovery_session_id)
