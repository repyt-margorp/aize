#!/usr/bin/env python3
"""Re-dispatch an idle AIze session so it produces a new turn.

Used after :file:`scripts/recover_panicked_session.py` has cleared a panic
audit_state. The session is already runnable on disk — this helper just
queues a fresh ``aize_goal_update`` pending input and sends a
``dispatch_pending`` message over the router socket so the target agent
service wakes up and consumes the queue.

Touches only:
  - .aize-state/sessions/<user>/<sid>/pending/                (new goal_update entry)
  - kernel.router unix socket                                 (dispatch_pending message)
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from kernel.auth import issue_auth_context  # noqa: E402
from kernel.ipc import connect_to_router  # noqa: E402
from runtime.message_builder import (  # noqa: E402
    make_aize_pending_input,
    make_dispatch_pending_message,
)
from runtime.persistent_state_pkg import (  # noqa: E402
    append_pending_input,
    get_session_settings,
)
from wire.protocol import encode_line  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--username", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument(
        "--to-service-id",
        required=True,
        help="Target agent service to wake (e.g. service-codex-001).",
    )
    parser.add_argument(
        "--from-service-id",
        default="service-http-001",
        help="Sender service_id used for the router handshake. Defaults to HttpBridge.",
    )
    parser.add_argument(
        "--runtime-root",
        default=str(REPO_ROOT / ".aize-runtime"),
        help="Path to the active runtime root (defaults to <repo>/.aize-runtime).",
    )
    parser.add_argument(
        "--reason",
        default="manual_resume",
        help="Dispatch reason recorded on the router message.",
    )
    parser.add_argument(
        "--note",
        default="Session re-dispatched after panic recovery.",
        help="Free-form note included in the queued goal_update input.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned actions but do not write pending input or send a router message.",
    )
    args = parser.parse_args(argv)

    runtime_root = Path(args.runtime_root).expanduser().resolve()
    manifest_path = runtime_root / "manifest.json"
    if not manifest_path.is_file():
        print(json.dumps({"error": "manifest_missing", "path": str(manifest_path)}, ensure_ascii=False))
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    talk = get_session_settings(runtime_root, username=args.username, session_id=args.session_id)
    if not isinstance(talk, dict):
        print(json.dumps({"error": "session_not_found"}, ensure_ascii=False))
        return 3

    goal_text = str(talk.get("goal_text") or "").strip()
    if not goal_text:
        print(json.dumps({"error": "no_goal_text"}, ensure_ascii=False))
        return 4
    active_goal_id = str(talk.get("active_goal_id") or talk.get("goal_id") or "").strip()

    lines = ["<aize_goal_update>"]
    if active_goal_id:
        lines.append(f"  <goal_id>{html.escape(active_goal_id)}</goal_id>")
    lines.append(f"  <goal_text>{html.escape(goal_text)}</goal_text>")
    if args.note:
        lines.append(f"  <note>{html.escape(args.note)}</note>")
    lines.append(
        "  <instruction>Resume work on the active goal. The previous turn panicked on a transient"
        " backend network failure; that condition has been cleared and the session is now"
        " runnable. Continue work toward the active goal until GoalManager can mark it"
        " completed.</instruction>"
    )
    lines.append("</aize_goal_update>")
    pending_text = "\n".join(lines)

    run_id = f"manual-resume-{uuid.uuid4().hex[:8]}"
    process_id = f"resume-script-{uuid.uuid4().hex[:8]}"

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "username": args.username,
                    "session_id": args.session_id,
                    "to_service_id": args.to_service_id,
                    "from_service_id": args.from_service_id,
                    "reason": args.reason,
                    "run_id": run_id,
                    "process_id": process_id,
                    "pending_input": pending_text,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    append_pending_input(
        runtime_root,
        username=args.username,
        session_id=args.session_id,
        entry=make_aize_pending_input(
            kind="goal_update",
            role="system",
            text=pending_text,
        ),
    )

    auth_context = issue_auth_context(runtime_root, username=args.username)
    message = make_dispatch_pending_message(
        manifest=manifest,
        from_service_id=args.from_service_id,
        to_service_id=args.to_service_id,
        process_id=process_id,
        run_id=run_id,
        username=args.username,
        session_id=args.session_id,
        auth_context=auth_context,
        reason=args.reason,
    )

    with connect_to_router(runtime_root, args.from_service_id) as conn:
        conn.write(encode_line(message))

    print(
        json.dumps(
            {
                "mode": "dispatched",
                "username": args.username,
                "session_id": args.session_id,
                "to_service_id": args.to_service_id,
                "from_service_id": args.from_service_id,
                "reason": args.reason,
                "run_id": run_id,
                "process_id": process_id,
                "message_id": message["meta"]["message_id"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
