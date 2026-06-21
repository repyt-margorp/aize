from __future__ import annotations

import time
from typing import Any

from store import Store
from store_defs import StoreError


def run_dispatch_loop(
    store: Store,
    *,
    limit: int | None,
    idle_rounds: int,
    interval: float,
    recovery_context: str | None = None,
) -> dict[str, Any]:
    if limit is not None and limit < 1:
        raise StoreError("dispatch limit must be positive")
    if idle_rounds < 1:
        raise StoreError("idle rounds must be positive")
    if interval < 0:
        raise StoreError("interval must not be negative")
    dispatched: list[dict[str, Any]] = []
    idle_count = 0
    while limit is None or len(dispatched) < limit:
        result = store.dispatch_once(recovery_context=recovery_context)
        if result is None:
            idle_count += 1
            if idle_count >= idle_rounds:
                break
            if interval:
                time.sleep(interval)
            continue
        idle_count = 0
        dispatched.append(result)
    return {
        "dispatched_count": len(dispatched),
        "idle_rounds": idle_count,
        "results": dispatched,
    }


def run_dispatch_worker(
    store: Store,
    *,
    session_id: str | None,
    max_dispatches: int | None,
    idle_timeout: float | None,
    interval: float,
    recovery_context: str | None = None,
) -> dict[str, Any]:
    if max_dispatches is not None and max_dispatches < 1:
        raise StoreError("max dispatches must be positive")
    if idle_timeout is not None and idle_timeout < 0:
        raise StoreError("idle timeout must not be negative")
    if interval < 0:
        raise StoreError("interval must not be negative")
    dispatched: list[dict[str, Any]] = []
    started = time.monotonic()
    last_dispatch = started
    idle_polls = 0
    while max_dispatches is None or len(dispatched) < max_dispatches:
        result = store.dispatch_once(session_id=session_id, recovery_context=recovery_context)
        if result is None:
            idle_polls += 1
            now = time.monotonic()
            if idle_timeout is not None and now - last_dispatch >= idle_timeout:
                break
            if interval:
                time.sleep(interval)
            continue
        last_dispatch = time.monotonic()
        idle_polls = 0
        dispatched.append(result)
    return {
        "dispatched_count": len(dispatched),
        "idle_polls": idle_polls,
        "session_id": session_id,
        "results": dispatched,
        "worker_elapsed_seconds": round(time.monotonic() - started, 3),
    }


def run_daemon(
    store: Store,
    *,
    parent_session_id: str,
    created_by: str,
    schedule_interval: float,
    dispatch_interval: float,
    max_cycles: int | None = None,
    idle_timeout: float | None = None,
    recovery_context: str | None = None,
) -> dict[str, Any]:
    if schedule_interval <= 0:
        raise StoreError("schedule interval must be positive")
    if dispatch_interval < 0:
        raise StoreError("dispatch interval must not be negative")
    if max_cycles is not None and max_cycles < 1:
        raise StoreError("max cycles must be positive")
    if idle_timeout is not None and idle_timeout < 0:
        raise StoreError("idle timeout must not be negative")

    store.init()
    started = time.monotonic()
    last_activity = started
    next_schedule_poll = 0.0
    cycle_count = 0
    idle_polls = 0
    scheduled: list[dict[str, Any]] = []
    dispatched: list[dict[str, Any]] = []

    while max_cycles is None or cycle_count < max_cycles:
        cycle_count += 1
        now = time.monotonic()
        if now >= next_schedule_poll:
            started_sessions = store.run_scheduled_units(
                parent_session_id=parent_session_id,
                created_by=created_by,
            )
            if started_sessions:
                scheduled.extend(started_sessions)
                last_activity = time.monotonic()
            next_schedule_poll = now + schedule_interval

        result = store.dispatch_once(recovery_context=recovery_context)
        if result is None:
            idle_polls += 1
            if idle_timeout is not None and time.monotonic() - last_activity >= idle_timeout:
                break
            if dispatch_interval:
                time.sleep(dispatch_interval)
            continue

        dispatched.append(result)
        last_activity = time.monotonic()
        idle_polls = 0

    return {
        "cycle_count": cycle_count,
        "scheduled_count": len(scheduled),
        "dispatched_count": len(dispatched),
        "idle_polls": idle_polls,
        "scheduled": scheduled,
        "results": dispatched,
        "daemon_elapsed_seconds": round(time.monotonic() - started, 3),
    }

