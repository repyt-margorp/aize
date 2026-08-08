from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cli_console import run_console
from cli_render import (
    DEFAULT_MESSAGE_LIMIT,
    agent_pool_snapshot,
    print_json,
    tail_items,
)
from cli_workers import run_daemon, run_dispatch_loop, run_dispatch_worker
from model import utc_now
from store import Store
from store_defs import StoreError

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="AIze")
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

    set_dispatch_lots = sub.add_parser("set-dispatch-lots", help="set daemon dispatch lot target size")
    set_dispatch_lots.add_argument("size", type=int)

    set_session_policy = sub.add_parser("set-session-policy", help="set Session scheduling class and base priority")
    set_session_policy.add_argument("session_id")
    set_session_policy.add_argument(
        "scheduling_class",
        choices=["idle", "background", "normal", "high", "critical"],
    )
    set_session_policy.add_argument("base_priority", type=int)

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
    create_unit.add_argument("--schedule-next-run-at")
    create_unit.add_argument(
        "--schedule-resolver",
        choices=["explicit", "next_interval_boundary"],
    )
    create_unit.add_argument("--schedule-fixed-parameters", default="{}")
    create_unit.add_argument("--owner-account", default="root")
    create_unit.add_argument("--manual", action="store_true", default=None, dest="manual")
    create_unit.add_argument("--no-manual", action="store_false", dest="manual")
    create_unit.add_argument("--startup", action="store_true", default=False)

    configure_schedule = sub.add_parser("configure-unit-schedule", help="set a Unit schedule resolver and fixed parameters")
    configure_schedule.add_argument("unit_id")
    configure_schedule.add_argument("resolver", choices=["explicit", "next_interval_boundary"])
    configure_schedule.add_argument("--fixed-parameters", default="{}")
    configure_schedule.add_argument("--next-run-at")
    configure_schedule.add_argument("--note")
    configure_schedule.add_argument("--disable", action="store_true")

    run_scheduled_units = sub.add_parser("run-scheduled-units", help="start due scheduled Unit sessions")
    run_scheduled_units.add_argument("--parent", default="root", dest="parent_session_id")
    run_scheduled_units.add_argument("--created-by", default="root", dest="created_by")
    run_scheduled_units.add_argument("--now")

    run_startup_units = sub.add_parser("run-startup-units", help="start Unit sessions that run on daemon startup")
    run_startup_units.add_argument("--parent", default="root", dest="parent_session_id")
    run_startup_units.add_argument("--created-by", default="root", dest="created_by")
    run_startup_units.add_argument("--now")

    create_session = sub.add_parser("create-session", help="create a session")
    create_session.add_argument("session_id")
    create_session.add_argument("--unit", dest="unit_id")
    create_session.add_argument("--parent", action="append", dest="parent_session_ids")
    create_session.add_argument("--created-by", dest="created_by")

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

    session_log = sub.add_parser("session-log", help="list session log entries")
    session_log.add_argument("session_id")
    session_log.add_argument("--from", dest="from_seq", type=int)
    session_log.add_argument("--to", dest="to_seq", type=int)
    session_log.add_argument("-n", "--limit", type=int, default=DEFAULT_MESSAGE_LIMIT, help="number of trailing log entries to show, 0 for all")
    session_log.add_argument("--role", choices=["GoalManager", "WorkerAgent"])
    session_log.add_argument("--after-cursor", action="store_true")

    goals = sub.add_parser("goals", help="list goals")
    goals.add_argument("session_id", nargs="?")

    dispatch_runs = sub.add_parser("dispatch-runs", help="list dispatch runs")
    dispatch_runs.add_argument("session_id", nargs="?")

    dispatch_readiness = sub.add_parser("dispatch-readiness", help="list role dispatch readiness")
    dispatch_readiness.add_argument("session_id", nargs="?")

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
    daemon.add_argument("--dispatch-lots", type=int, default=1)
    daemon.add_argument("--max-dispatch-lots", type=int)
    daemon.add_argument("--max-cycles", type=int)
    daemon.add_argument("--idle-timeout", type=float)
    daemon.add_argument("--recovery-context")

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
            print_json(agent_pool_snapshot(store.agent_profiles(), store.dispatch_runs(include_output=False)))
        elif args.command == "set-dispatch-lots":
            print_json(store.set_dispatch_lot_size(args.size))
        elif args.command == "set-session-policy":
            print_json(
                store.set_session_scheduling_policy(
                    args.session_id,
                    scheduling_class=args.scheduling_class,
                    base_priority=args.base_priority,
                )
            )
        elif args.command == "set-agent":
            print_json(store.set_agent_provider(args.role, provider=args.provider))
        elif args.command == "auth":
            print_json(store.authenticate(args.username, password=args.password))
        elif args.command == "create-account":
            print_json(store.create_account(args.username, password=args.password, roles=args.roles))
        elif args.command == "create-unit":
            schedule = {}
            if (
                args.schedule_every_hours is not None
                or args.schedule_next_run_at is not None
                or args.schedule_resolver is not None
            ):
                try:
                    fixed_parameters = json.loads(args.schedule_fixed_parameters)
                except json.JSONDecodeError as exc:
                    raise StoreError(f"invalid schedule fixed parameters JSON: {exc}") from exc
                if not isinstance(fixed_parameters, dict):
                    raise StoreError("schedule fixed parameters must be a JSON object")
                resolver = args.schedule_resolver or "explicit"
                if args.schedule_every_hours is not None:
                    resolver = "next_interval_boundary"
                    fixed_parameters = {
                        **fixed_parameters,
                        "interval_seconds": args.schedule_every_hours * 3600,
                        "anchor": str(fixed_parameters.get("anchor") or "scheduled_for"),
                    }
                schedule = {
                    "enabled": True,
                    "next_run_at": args.schedule_next_run_at or utc_now(),
                    "resolver": resolver,
                    "fixed_parameters": fixed_parameters,
                }
            activation_triggers = {
                "manual": True if args.manual is None else bool(args.manual),
                "scheduled": bool(schedule),
                "startup": bool(args.startup),
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
                    activation_triggers=activation_triggers,
                    owner_account=args.owner_account,
                ).to_dict()
            )
        elif args.command == "configure-unit-schedule":
            try:
                fixed_parameters = json.loads(args.fixed_parameters)
            except json.JSONDecodeError as exc:
                raise StoreError(f"invalid schedule fixed parameters JSON: {exc}") from exc
            if not isinstance(fixed_parameters, dict):
                raise StoreError("schedule fixed parameters must be a JSON object")
            print_json(
                store.configure_unit_schedule(
                    args.unit_id,
                    resolver=args.resolver,
                    fixed_parameters=fixed_parameters,
                    next_run_at=args.next_run_at,
                    note=args.note,
                    enabled=not args.disable,
                )
            )
        elif args.command == "run-scheduled-units":
            print_json(
                store.run_scheduled_units(
                    parent_session_id=args.parent_session_id,
                    created_by=args.created_by,
                    now=args.now,
                )
            )
        elif args.command == "run-startup-units":
            print_json(
                store.run_startup_units(
                    parent_session_id=args.parent_session_id,
                    created_by=args.created_by,
                    now=args.now,
                )
            )
        elif args.command == "create-session":
            parent_session_ids = args.parent_session_ids
            if parent_session_ids is None and args.created_by:
                parent_session_ids = [store.account_home_session(args.created_by)]
            print_json(
                store.create_session(
                    args.session_id,
                    unit_id=args.unit_id,
                    parent_session_ids=parent_session_ids,
                )
            )
        elif args.command == "activate-session":
            print_json(store.set_session_active(args.session_id, active=True))
        elif args.command == "deactivate-session":
            print_json(store.set_session_active(args.session_id, active=False))
        elif args.command == "start-goal":
            parent_session_ids = args.parent_session_ids or [store.account_home_session(args.created_by)]
            print_json(
                store.start_goal_session(
                    args.session_id,
                    unit_id=args.unit_id,
                    parent_session_ids=parent_session_ids,
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
        elif args.command == "session-log":
            print_json(
                store.session_log(
                    args.session_id,
                    from_seq=args.from_seq,
                    to_seq=args.to_seq,
                    limit=args.limit,
                    role=args.role,
                    after_cursor=args.after_cursor,
                )
            )
        elif args.command == "goals":
            print_json(store.goals(args.session_id))
        elif args.command == "dispatch-runs":
            print_json(store.dispatch_runs(args.session_id))
        elif args.command == "dispatch-readiness":
            print_json(store.dispatch_readiness(args.session_id))
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
                    dispatch_lots=args.dispatch_lots,
                    max_dispatch_lots=args.max_dispatch_lots,
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
