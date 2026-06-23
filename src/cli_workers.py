from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
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
    dispatch_lots: int = 1,
    max_dispatch_lots: int | None = None,
    max_cycles: int | None = None,
    idle_timeout: float | None = None,
    recovery_context: str | None = None,
) -> dict[str, Any]:
    if schedule_interval <= 0:
        raise StoreError("schedule interval must be positive")
    if dispatch_interval < 0:
        raise StoreError("dispatch interval must not be negative")
    if dispatch_lots < 1:
        raise StoreError("dispatch lots must be positive")
    if max_dispatch_lots is not None and max_dispatch_lots < 1:
        raise StoreError("max dispatch lots must be positive")
    if max_cycles is not None and max_cycles < 1:
        raise StoreError("max cycles must be positive")
    if idle_timeout is not None and idle_timeout < 0:
        raise StoreError("idle timeout must not be negative")

    store.init()
    lot_cap = max_dispatch_lots or max(dispatch_lots, 10)
    if lot_cap < dispatch_lots:
        raise StoreError("max dispatch lots must be greater than or equal to dispatch lots")
    store.set_dispatch_lot_size(dispatch_lots)
    started = time.monotonic()
    last_activity = started
    next_schedule_poll = 0.0
    cycle_count = 0
    idle_polls = 0
    scheduled: list[dict[str, Any]] = []
    dispatched: list[dict[str, Any]] = []
    active_lots: dict[int, Future[dict[str, Any] | None]] = {}
    peak_active_lots = 0

    with ThreadPoolExecutor(max_workers=lot_cap, thread_name_prefix="aize-dispatch-lot") as executor:
        while max_cycles is None or cycle_count < max_cycles:
            cycle_count += 1
            dispatched_before = len(dispatched)
            completed_lots = _collect_completed_lots(active_lots, dispatched)
            completed_dispatches = len(dispatched) - dispatched_before
            if completed_dispatches:
                last_activity = time.monotonic()
                idle_polls = 0

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

            target_lots = min(store.dispatch_lot_size(), lot_cap)
            _submit_available_lots(
                executor,
                active_lots,
                target_lots=target_lots,
                recovery_context=recovery_context,
                store=store,
            )
            if active_lots:
                peak_active_lots = max(peak_active_lots, len(active_lots))
            if not completed_dispatches:
                idle_polls += 1
                if idle_timeout is not None and time.monotonic() - last_activity >= idle_timeout:
                    break
                if dispatch_interval:
                    time.sleep(dispatch_interval)

        for future in list(active_lots.values()):
            future.result()
        _collect_completed_lots(active_lots, dispatched)

    return {
        "cycle_count": cycle_count,
        "scheduled_count": len(scheduled),
        "dispatched_count": len(dispatched),
        "dispatch_lot_size": store.dispatch_lot_size(),
        "dispatch_lot_cap": lot_cap,
        "active_dispatch_lots": len(active_lots),
        "peak_active_dispatch_lots": peak_active_lots,
        "idle_polls": idle_polls,
        "scheduled": scheduled,
        "results": dispatched,
        "daemon_elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _submit_available_lots(
    executor: ThreadPoolExecutor,
    active_lots: dict[int, Future[dict[str, Any] | None]],
    *,
    target_lots: int,
    recovery_context: str | None,
    store: Store,
) -> list[int]:
    submitted: list[int] = []
    if target_lots < 1:
        return submitted
    for lot_id in range(1, target_lots + 1):
        if len(active_lots) >= target_lots:
            break
        if lot_id in active_lots:
            continue
        active_lots[lot_id] = executor.submit(
            store.dispatch_once,
            recovery_context=recovery_context,
            dispatch_lot_id=lot_id,
        )
        submitted.append(lot_id)
    return submitted


def _collect_completed_lots(
    active_lots: dict[int, Future[dict[str, Any] | None]],
    dispatched: list[dict[str, Any]],
) -> list[int]:
    completed: list[int] = []
    for lot_id, future in list(active_lots.items()):
        if not future.done():
            continue
        result = future.result()
        if result is not None:
            dispatched.append(result)
        completed.append(lot_id)
        active_lots.pop(lot_id, None)
    return completed
