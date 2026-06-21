from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .cli_console import run_console
from .cli_render import (
    DEFAULT_MESSAGE_LIMIT,
    agent_pool_snapshot,
    print_json,
    tail_items,
)
from .cli_workers import run_dispatch_loop, run_dispatch_worker
from .store import Store
from .store_defs import StoreError

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="new-aize")
    parser.add_argument(
        "--root",
        default=".new-aize-state",
        help="runtime state directory, default: .new-aize-state",
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

    dispatch_queue = sub.add_parser("dispatch-queue", help="list dispatch queue entries")
    dispatch_queue.add_argument("session_id", nargs="?")

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

    dispatch_worker = sub.add_parser("dispatch-worker", help="poll the dispatch queue and dispatch new work")
    dispatch_worker.add_argument("--session", dest="session_id")
    dispatch_worker.add_argument("--max-dispatches", type=int)
    dispatch_worker.add_argument("--idle-timeout", type=float)
    dispatch_worker.add_argument("--interval", type=float, default=1.0)
    dispatch_worker.add_argument("--recovery-context")

    console = sub.add_parser("console", help="login and operate sessions interactively")
    console.add_argument("--username")
    console.add_argument("--password")

    return parser


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
            print_json(
                store.create_unit(
                    args.unit_id,
                    instance_policy=args.instance_policy,
                ).to_dict()
            )
        elif args.command == "create-session":
            print_json(
                store.create_session(
                    args.session_id,
                    unit_id=args.unit_id,
                    parent_session_ids=args.parent_session_ids,
                ).to_dict()
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
        elif args.command == "dispatch-queue":
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
