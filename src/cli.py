from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cli_console import run_console
from cli_render import (
    DEFAULT_MESSAGE_LIMIT,
    agent_pool_snapshot,
    print_json,
    tail_items,
)
from cli_workers import run_daemon, run_dispatch_loop, run_dispatch_worker
from store import Store
from store_defs import StoreError

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aize")
    parser.add_argument(
        "--root",
        default=".aize-state",
        help="runtime state directory, default: .aize-state",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="initialize runtime state")
    sub.add_parser("status", help="show system status counts")
    sub.add_parser("units", help="list units")
    sub.add_parser("sessions", help="list sessions")
    sub.add_parser("accounts", help="list accounts without password hashes")
    sub.add_parser("agents", help="list agent role provider assignments")
    sub.add_parser("agent-pool", help="show active agent pool allocations")

    set_agent = sub.add_parser("set-agent", help="assign a provider to an agent role")
    set_agent.add_argument("role")
    set_agent.add_argument("provider")

    auth = sub.add_parser("auth", help="authenticate an account")
    auth.add_argument("username")
    auth.add_argument("password")

    create_account = sub.add_parser("create-account", help="create an account")
    create_account.add_argument("username")
    create_account.add_argument("password")
    create_account.add_argument("--role", action="append", dest="roles")

    create_unit = sub.add_parser("create-unit", help="create a unit")
    create_unit.add_argument("unit_id")
    create_unit.add_argument("--instance-policy", choices=["multi", "singleton"], default="multi")
    create_unit.add_argument("--display-name", default="")
    create_unit.add_argument("--description", default="")
    create_unit.add_argument("--goal-text", default="")
    create_unit.add_argument("--initial-prompt", default="")
    create_unit.add_argument("--schedule-every-hours", type=int)
    create_unit.add_argument("--schedule-every-seconds", type=int)
    create_unit.add_argument("--schedule-next-run-at")
    create_unit.add_argument("--automation-command", action="append", dest="automation_command")
    create_unit.add_argument("--automation-cwd", default="")
    create_unit.add_argument("--automation-timeout-seconds", type=int, default=900)

    sync_app_units = sub.add_parser("sync-app-units", help="sync enabled plugin launcher apps into Units")
    sync_app_units.add_argument("--plugins-dir", default="plugins/private")
    sync_app_units.add_argument("--app-id", action="append", dest="app_ids")

    run_scheduled_units = sub.add_parser("run-scheduled-units", help="start due scheduled Unit sessions")
    run_scheduled_units.add_argument("--parent", default="root", dest="parent_session_id")
    run_scheduled_units.add_argument("--created-by", default="root", dest="created_by")
    run_scheduled_units.add_argument("--now")

    create_session = sub.add_parser("create-session", help="create a session")
    create_session.add_argument("session_id")
    create_session.add_argument("--unit", dest="unit_id")
    create_session.add_argument("--parent", action="append", dest="parent_session_ids")

    activate_session = sub.add_parser("activate-session", help="set a session active")
    activate_session.add_argument("session_id")

    deactivate_session = sub.add_parser("deactivate-session", help="set a session inactive")
    deactivate_session.add_argument("session_id")

    start_goal = sub.add_parser("start-goal", help="create a child session and active goal")
    start_goal.add_argument("session_id")
    start_goal.add_argument("--unit", dest="unit_id")
    start_goal.add_argument("--parent", action="append", dest="parent_session_ids")
    start_goal.add_argument("--label", required=True)
    start_goal.add_argument("--body", default="")
    start_goal.add_argument("--created-by", required=True, dest="created_by")

    update_goal = sub.add_parser("update-goal", help="set or update the SessionGoal body for an existing session")
    update_goal.add_argument("session_id")
    update_goal.add_argument("body", nargs="?")
    update_goal.add_argument("--body", dest="body_option")
    update_goal.add_argument("--created-by", required=True, dest="created_by")

    send = sub.add_parser("send", help="append a queued message to a session")
    send.add_argument("session_id")
    send.add_argument("sender")
    send.add_argument("recipient")
    send.add_argument("body")

    send_file = sub.add_parser("send-file", help="append a queued message with a file payload")
    send_file.add_argument("session_id")
    send_file.add_argument("sender")
    send_file.add_argument("recipient")
    send_file.add_argument("path")
    send_file.add_argument("--body", default="file payload")
    send_file.add_argument("--content-type", default="text/plain")

    user_input = sub.add_parser("user-input", help="append user input to a Session")
    user_input.add_argument("session_id")
    user_input.add_argument("sender")
    user_input.add_argument("body")
    user_input.add_argument("--reply-to")

    recv = sub.add_parser("recv", help="receive the next queued message for a recipient")
    recv.add_argument("recipient")
    recv.add_argument("--session", dest="session_id")

    link_session = sub.add_parser("link-session", help="add a parent-to-child session DAG edge")
    link_session.add_argument("parent_session_id")
    link_session.add_argument("child_session_id")

    sub.add_parser("session-graph", help="show session DAG nodes and edges")

    parents = sub.add_parser("parents", help="show parent session edges")
    parents.add_argument("session_id")

    children = sub.add_parser("children", help="show child session edges")
    children.add_argument("session_id")

    messages = sub.add_parser("messages", help="list messages")
    messages.add_argument("session_id", nargs="?")
    messages.add_argument("-n", "--limit", type=int, default=DEFAULT_MESSAGE_LIMIT, help="number of trailing messages to show, 0 for all")

    goals = sub.add_parser("goals", help="list goals")
    goals.add_argument("session_id", nargs="?")

    dispatch_runs = sub.add_parser("dispatch-runs", help="list dispatch runs")
    dispatch_runs.add_argument("session_id", nargs="?")

    dispatch_index = sub.add_parser("dispatch-index", help="list dispatch scheduling index entries")
    dispatch_index.add_argument("session_id", nargs="?")

    agent_threads = sub.add_parser("agent-threads", help="list durable session agent threads")
    agent_threads.add_argument("session_id", nargs="?")

    dispatch_once = sub.add_parser("dispatch-once", help="dispatch one active goal")
    dispatch_once.add_argument("--recovery-context")

    dispatch = sub.add_parser("dispatch", help="dispatch active goals")
    dispatch.add_argument("--limit", type=int)
    dispatch.add_argument("--recovery-context")

    dispatch_loop = sub.add_parser("dispatch-loop", help="run batch dispatch until idle")
    dispatch_loop.add_argument("--limit", type=int)
    dispatch_loop.add_argument("--idle-rounds", type=int, default=1)
    dispatch_loop.add_argument("--interval", type=float, default=0.0)
    dispatch_loop.add_argument("--recovery-context")

    dispatch_worker = sub.add_parser("dispatch-worker", help="poll the dispatch index and dispatch new work")
    dispatch_worker.add_argument("--session", dest="session_id")
    dispatch_worker.add_argument("--max-dispatches", type=int)
    dispatch_worker.add_argument("--idle-timeout", type=float)
    dispatch_worker.add_argument("--interval", type=float, default=1.0)
    dispatch_worker.add_argument("--recovery-context")

    daemon = sub.add_parser("daemon", help="run scheduled Units and dispatch work continuously")
    daemon.add_argument("--parent", default="root", dest="parent_session_id")
    daemon.add_argument("--created-by", default="root", dest="created_by")
    daemon.add_argument("--schedule-interval", type=float, default=60.0)
    daemon.add_argument("--dispatch-interval", type=float, default=1.0)
    daemon.add_argument("--no-dispatch", action="store_true", help="run scheduled Units without dispatching queued work")
    daemon.add_argument("--max-cycles", type=int)
    daemon.add_argument("--idle-timeout", type=float)
    daemon.add_argument("--recovery-context")

    console = sub.add_parser("console", help="login and operate sessions interactively")
    console.add_argument("--username")
    console.add_argument("--password")

    return parser


def _launcher_schedule(launcher: dict[str, Any], existing_unit: dict[str, Any] | None = None) -> dict[str, Any]:
    schedule_config = launcher.get("schedule")
    if not isinstance(schedule_config, dict):
        return {}
    interval = schedule_config.get("interval")
    if not isinstance(interval, dict) or interval.get("enabled") is not True:
        return {}
    every_hours = int(interval.get("hours") or 0)
    every_seconds = int(interval.get("seconds") or interval.get("every_seconds") or 0)
    if every_hours < 1 and every_seconds < 1:
        return {}
    existing_schedule = existing_unit.get("schedule") if isinstance(existing_unit, dict) else None
    next_run_at = ""
    if isinstance(existing_schedule, dict):
        next_run_at = str(existing_schedule.get("next_run_at") or "").strip()
    return {
        "enabled": True,
        "kind": "interval",
        "every_hours": every_hours,
        "every_seconds": every_seconds,
        "next_run_at": next_run_at or None,
        "timezone": "UTC",
    }


def _launcher_automation(launcher: dict[str, Any]) -> dict[str, Any]:
    automation = launcher.get("automation")
    if not isinstance(automation, dict) or automation.get("enabled") is not True:
        return {}
    return {
        "enabled": True,
        "command": automation.get("command") or [],
        "cwd": automation.get("cwd") or "",
        "timeout_seconds": int(automation.get("timeout_seconds") or 900),
    }


def sync_app_units(store: Store, plugins_dir: Path, app_ids: list[str] | None = None) -> list[dict[str, Any]]:
    selected = set(app_ids or [])
    synced: list[dict[str, Any]] = []
    existing_units = {unit["unit_id"]: unit for unit in store.units()}
    for app_path in sorted(plugins_dir.glob("*/apps/*/app.json")):
        app = json.loads(app_path.read_text(encoding="utf-8"))
        app_id = str(app.get("app_id") or "").strip()
        if not app_id or (selected and app_id not in selected):
            continue
        if app.get("enabled") is not True:
            continue
        launcher = app.get("launcher")
        if not isinstance(launcher, dict):
            continue
        unit = store.upsert_unit(
            app_id,
            instance_policy="multi",
            display_name=str(app.get("display_name") or app_id),
            description=str(app.get("description") or ""),
            goal_text=str(launcher.get("goal_text") or app.get("description") or app_id),
            initial_prompt=str(launcher.get("initial_prompt") or ""),
            schedule=_launcher_schedule(launcher, existing_units.get(app_id)),
            automation=_launcher_automation(launcher),
        )
        synced.append({"app_path": str(app_path), "unit": unit})
    return synced


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(Path(args.root))
    try:
        if args.command == "init":
            print_json(store.init())
        elif args.command == "status":
            print_json(store.status())
        elif args.command == "units":
            print_json(store.units())
        elif args.command == "sessions":
            print_json(store.sessions())
        elif args.command == "accounts":
            print_json(store.accounts())
        elif args.command == "agents":
            print_json(store.agent_profiles())
        elif args.command == "agent-pool":
            print_json(agent_pool_snapshot(store.agent_profiles(), store.dispatch_runs()))
        elif args.command == "set-agent":
            print_json(store.set_agent_provider(args.role, provider=args.provider))
        elif args.command == "auth":
            print_json(store.authenticate(args.username, password=args.password))
        elif args.command == "create-account":
            print_json(store.create_account(args.username, password=args.password, roles=args.roles))
        elif args.command == "create-unit":
            schedule = {}
            if args.schedule_every_hours is not None or args.schedule_every_seconds is not None:
                schedule = {
                    "enabled": True,
                    "kind": "interval",
                    "every_hours": args.schedule_every_hours or 0,
                    "every_seconds": args.schedule_every_seconds or 0,
                    "next_run_at": args.schedule_next_run_at,
                    "timezone": "UTC",
                }
            automation = {}
            if args.automation_command:
                automation = {
                    "enabled": True,
                    "command": args.automation_command,
                    "cwd": args.automation_cwd,
                    "timeout_seconds": args.automation_timeout_seconds,
                }
            print_json(
                store.create_unit(
                    args.unit_id,
                    instance_policy=args.instance_policy,
                    display_name=args.display_name,
                    description=args.description,
                    goal_text=args.goal_text,
                    initial_prompt=args.initial_prompt,
                    schedule=schedule,
                    automation=automation,
                ).to_dict()
            )
        elif args.command == "sync-app-units":
            plugins_dir = Path(args.plugins_dir)
            if not plugins_dir.is_absolute():
                plugins_dir = Path.cwd() / plugins_dir
            print_json(sync_app_units(store, plugins_dir, args.app_ids))
        elif args.command == "run-scheduled-units":
            print_json(
                store.run_scheduled_units(
                    parent_session_id=args.parent_session_id,
                    created_by=args.created_by,
                    now=args.now,
                )
            )
        elif args.command == "create-session":
            print_json(
                store.create_session(
                    args.session_id,
                    unit_id=args.unit_id,
                    parent_session_ids=args.parent_session_ids,
                )
            )
        elif args.command == "activate-session":
            print_json(store.set_session_active(args.session_id, active=True))
        elif args.command == "deactivate-session":
            print_json(store.set_session_active(args.session_id, active=False))
        elif args.command == "start-goal":
            print_json(
                store.start_goal_session(
                    args.session_id,
                    unit_id=args.unit_id,
                    parent_session_ids=args.parent_session_ids or ["root"],
                    label=args.label,
                    body=args.body,
                    created_by=args.created_by,
                )
            )
        elif args.command == "update-goal":
            goal_body = args.body_option if args.body_option is not None else args.body
            print_json(
                store.update_goal(
                    args.session_id,
                    body=goal_body or "",
                    created_by=args.created_by,
                )
            )
        elif args.command == "send":
            print_json(
                store.append_message(
                    args.session_id,
                    sender=args.sender,
                    recipient=args.recipient,
                    body=args.body,
                )
            )
        elif args.command == "send-file":
            path = Path(args.path)
            print_json(
                store.append_file_message(
                    args.session_id,
                    sender=args.sender,
                    recipient=args.recipient,
                    body=args.body,
                    file_name=path.name,
                    content=path.read_text(encoding="utf-8"),
                    content_type=args.content_type,
                )
            )
        elif args.command == "user-input":
            print_json(
                store.append_user_input(
                    args.session_id,
                    sender=args.sender,
                    body=args.body,
                    reply_to=args.reply_to,
                )
            )
        elif args.command == "recv":
            message = store.receive_message(args.recipient, session_id=args.session_id)
            print_json(message if message is not None else {"message": None})
        elif args.command == "link-session":
            print_json(store.link_sessions(args.parent_session_id, args.child_session_id))
        elif args.command == "session-graph":
            print_json(store.session_graph())
        elif args.command == "parents":
            print_json(store.parents(args.session_id))
        elif args.command == "children":
            print_json(store.children(args.session_id))
        elif args.command == "messages":
            print_json(tail_items(store.messages(args.session_id), args.limit))
        elif args.command == "goals":
            print_json(store.goals(args.session_id))
        elif args.command == "dispatch-runs":
            print_json(store.dispatch_runs(args.session_id))
        elif args.command == "dispatch-index":
            print_json(store.dispatch_queue(args.session_id))
        elif args.command == "agent-threads":
            print_json(store.agent_threads(args.session_id))
        elif args.command == "dispatch-once":
            print_json(store.dispatch_once(recovery_context=args.recovery_context) or {"dispatched": None})
        elif args.command == "dispatch":
            print_json(store.dispatch(limit=args.limit, recovery_context=args.recovery_context))
        elif args.command == "dispatch-loop":
            print_json(
                run_dispatch_loop(
                    store,
                    limit=args.limit,
                    idle_rounds=args.idle_rounds,
                    interval=args.interval,
                    recovery_context=args.recovery_context,
                )
            )
        elif args.command == "dispatch-worker":
            print_json(
                run_dispatch_worker(
                    store,
                    session_id=args.session_id,
                    max_dispatches=args.max_dispatches,
                    idle_timeout=args.idle_timeout,
                    interval=args.interval,
                    recovery_context=args.recovery_context,
                )
            )
        elif args.command == "daemon":
            print_json(
                run_daemon(
                    store,
                    parent_session_id=args.parent_session_id,
                    created_by=args.created_by,
                    schedule_interval=args.schedule_interval,
                    dispatch_interval=args.dispatch_interval,
                    dispatch_enabled=not args.no_dispatch,
                    max_cycles=args.max_cycles,
                    idle_timeout=args.idle_timeout,
                    recovery_context=args.recovery_context,
                )
            )
        elif args.command == "console":
            return run_console(store, username=args.username, password=args.password)
        else:
            raise StoreError(f"unknown command: {args.command}")
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
