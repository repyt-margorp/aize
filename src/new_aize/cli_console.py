from __future__ import annotations

import getpass
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .cli_render import (
    DEFAULT_MESSAGE_LIMIT,
    agent_pool_snapshot,
    console_body,
    print_agent_pool,
    print_agent_threads,
    print_created_goal_session,
    print_created_session,
    print_dispatch_queue,
    print_dispatch_result,
    print_dispatch_runs,
    print_goals,
    print_graph,
    print_messages,
    print_session,
    print_units,
    tail_items,
)
from .model import new_id
from .store import Store
from .store_defs import StoreError, payload_body

CLI_STARTUP_RECOVERY_CONTEXT = (
    "The AIZE CLI console started or restarted. Treat this as a runtime resume point: "
    "persisted state may have changed since the previous dispatch, so continue working "
    "toward the current SessionGoal using the current Session messages and goal state."
)


def start_console_message_poller(
    store: Store,
    *,
    current_session: dict[str, str],
    console_endpoint_id: str,
    stop_event: threading.Event,
) -> threading.Thread:
    def is_for_this_console(message: dict[str, Any]) -> bool:
        return message.get("to") == f"console:{console_endpoint_id}"

    seen_message_ids = {
        str(message.get("message_id") or "")
        for message in store.messages()
        if is_for_this_console(message)
    }

    def poll() -> None:
        while not stop_event.is_set():
            try:
                session_id = current_session["session_id"]
                messages = store.messages(session_id)
                for message in messages:
                    message_id = str(message.get("message_id") or "")
                    if not message_id or message_id in seen_message_ids:
                        continue
                    if not is_for_this_console(message):
                        continue
                    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                    if str(payload.get("dispatch_step") or "").endswith("Precheck"):
                        seen_message_ids.add(message_id)
                        continue
                    seen_message_ids.add(message_id)
                    print(
                        "\n"
                        f"[{session_id}] {message.get('from')}: "
                        f"{console_body(payload_body(message))}"
                    )
                    print(f"aize:{session_id}> ", end="", flush=True)
            except Exception:
                pass
            stop_event.wait(0.5)

    thread = threading.Thread(target=poll, name="aize-console-message-poller", daemon=True)
    thread.start()
    return thread


def start_background_dispatch(
    store: Store,
    *,
    session_id: str | None = None,
    max_dispatches: int | None = 1,
    idle_timeout: float | None = 5.0,
    interval: float = 0.05,
    recovery_context: str | None = None,
) -> subprocess.Popen[str]:
    log_dir = store.root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "dispatch-worker.log"
    log_file = log_path.open("a", encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "new_aize.cli",
        "--root",
        str(store.root),
        "dispatch-worker",
    ]
    if session_id:
        command.extend(["--session", session_id])
    if max_dispatches is not None:
        command.extend(["--max-dispatches", str(max_dispatches)])
    if idle_timeout is not None:
        command.extend(["--idle-timeout", str(idle_timeout)])
    command.extend(["--interval", str(interval)])
    if recovery_context:
        command.extend(["--recovery-context", recovery_context])
    process = subprocess.Popen(
        command,
        stdout=log_file,
        stderr=log_file,
        text=True,
        close_fds=True,
        start_new_session=True,
    )
    log_file.close()
    return process


def start_startup_dispatch_if_needed(store: Store) -> subprocess.Popen[str] | None:
    status = store.status()
    if int(status.get("queued_dispatch_count") or 0) < 1:
        return None
    if int(status.get("acquired_dispatch_count") or 0) > 0:
        return None
    if int(status.get("acquired_dispatch_lease_count") or 0) > 0:
        return None
    return start_background_dispatch(
        store,
        session_id=None,
        max_dispatches=None,
        idle_timeout=0.5,
        interval=0.05,
        recovery_context=CLI_STARTUP_RECOVERY_CONTEXT,
    )


def run_console(store: Store, *, username: str | None, password: str | None) -> int:
    store.init()
    login_username = username or input("username: ")
    login_password = password or getpass.getpass("password: ")
    account = store.authenticate(login_username, password=login_password)
    current_session_id = "root"
    current_session = {"session_id": current_session_id}
    console_endpoint_id = new_id("console")
    stop_event = threading.Event()
    start_console_message_poller(
        store,
        current_session=current_session,
        console_endpoint_id=console_endpoint_id,
        stop_event=stop_event,
    )
    print(f"logged in as {account['username']}")
    print(f"current session: {current_session_id}")
    print("type 'help' for commands")
    startup_process = start_startup_dispatch_if_needed(store)
    if startup_process:
        print(f"startup dispatch worker queued: pid={startup_process.pid}")

    try:
        while True:
            try:
                line = input(f"aize:{current_session_id}> ")
            except EOFError:
                print()
                return 0
            line = line.strip()
            if not line:
                continue
            try:
                parts = shlex.split(line)
            except ValueError as exc:
                print(f"error: {exc}")
                continue
            command = parts[0]
            args = parts[1:]
            try:
                if command in {"exit", "quit"}:
                    return 0
                if command == "help":
                    print("commands: session SESSION, unit-session SESSION UNIT, sessions, use SESSION, current, activate, deactivate, send BODY, send-file RECIPIENT PATH [BODY], messages [N], goals, update-goal BODY, agent-threads, agent-pool, dispatch-runs, dispatch-queue, goal SESSION LABEL [BODY], unit-goal SESSION UNIT LABEL [BODY], dispatch, graph, exit")
                elif command == "units":
                    print_units(store.units())
                elif command == "create-unit":
                    if len(args) != 1:
                        raise StoreError("usage: create-unit UNIT")
                    print_units([store.create_unit(args[0]).to_dict()])
                elif command == "session":
                    if len(args) != 1:
                        raise StoreError("usage: session SESSION")
                    print_created_session(
                        store.create_session(
                            args[0],
                            parent_session_ids=[current_session_id],
                        ).to_dict()
                    )
                elif command == "unit-session":
                    if len(args) != 2:
                        raise StoreError("usage: unit-session SESSION UNIT")
                    print_created_session(
                        store.create_session(
                            args[0],
                            unit_id=args[1],
                            parent_session_ids=[current_session_id],
                        ).to_dict()
                    )
                elif command == "sessions":
                    print_sessions(store.sessions())
                elif command == "current":
                    print_session(store.session(current_session_id))
                elif command == "activate":
                    print_session(store.set_session_active(current_session_id, active=True))
                elif command == "deactivate":
                    print_session(store.set_session_active(current_session_id, active=False))
                elif command == "use":
                    if len(args) != 1:
                        raise StoreError("usage: use SESSION")
                    store.session(args[0])
                    current_session_id = args[0]
                    current_session["session_id"] = current_session_id
                    print(f"current session: {current_session_id}")
                elif command == "send":
                    if len(args) < 1:
                        raise StoreError("usage: send BODY")
                    message = store.append_user_input(
                        current_session_id,
                        sender=str(account["username"]),
                        body=" ".join(args),
                        reply_to=console_endpoint_id,
                    )
                    print_messages(
                        [
                            message
                        ]
                    )
                    process = start_background_dispatch(store, session_id=current_session_id)
                    print(f"dispatch queued in background: pid={process.pid}")
                elif command == "send-file":
                    if len(args) < 2:
                        raise StoreError("usage: send-file RECIPIENT PATH [BODY]")
                    path = Path(args[1])
                    content = path.read_text(encoding="utf-8")
                    print_messages(
                        [
	                            store.append_file_message(
	                                current_session_id,
	                                sender=str(account["username"]),
	                                recipient=args[0],
	                                body=" ".join(args[2:]) or "file payload",
	                                file_name=path.name,
	                                content=content,
	                            )
                        ]
                    )
                elif command == "messages":
                    if len(args) > 1:
                        raise StoreError("usage: messages [N]")
                    limit = DEFAULT_MESSAGE_LIMIT
                    if args:
                        try:
                            limit = int(args[0])
                        except ValueError as exc:
                            raise StoreError("usage: messages [N]") from exc
                    print_messages(tail_items(store.messages(current_session_id), limit))
                elif command == "goals":
                    print_goals(store.goals(current_session_id))
                elif command == "update-goal":
                    if len(args) < 1:
                        raise StoreError("usage: update-goal BODY")
                    print_goals(
                        [
                            store.update_goal(
                                current_session_id,
                                body=" ".join(args),
                                created_by=str(account["username"]),
                            )
                        ]
                    )
                elif command == "agent-threads":
                    print_agent_threads(store.agent_threads(current_session_id))
                elif command == "agent-pool":
                    print_agent_pool(agent_pool_snapshot(store.agent_profiles(), store.dispatch_runs()))
                elif command == "dispatch-runs":
                    print_dispatch_runs(store.dispatch_runs(current_session_id))
                elif command == "dispatch-queue":
                    print_dispatch_queue(store.dispatch_queue(current_session_id))
                elif command == "dispatch":
                    print_dispatch_result(store.dispatch_once())
                elif command in {"goal", "start-goal"}:
                    if len(args) < 2:
                        raise StoreError("usage: goal CHILD_SESSION LABEL [BODY]")
                    print_created_goal_session(
                        store.start_goal_session(
                            args[0],
                            parent_session_ids=[current_session_id],
                            label=args[1],
                            body=" ".join(args[2:]),
                            created_by=str(account["username"]),
                        )
                    )
                elif command == "unit-goal":
                    if len(args) < 3:
                        raise StoreError("usage: unit-goal CHILD_SESSION UNIT LABEL [BODY]")
                    print_created_goal_session(
                        store.start_goal_session(
                            args[0],
                            unit_id=args[1],
                            parent_session_ids=[current_session_id],
                            label=args[2],
                            body=" ".join(args[3:]),
                            created_by=str(account["username"]),
                        )
                    )
                elif command == "graph":
                    print_graph(
                        store.session_graph(),
                        goals=store.goals(),
                        queue=store.dispatch_queue(),
                        runs=store.dispatch_runs(),
                    )
                else:
                    raise StoreError(f"unknown console command: {command}")
            except StoreError as exc:
                print(f"error: {exc}")
    finally:
        stop_event.set()
