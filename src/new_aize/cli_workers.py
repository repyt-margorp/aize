from __future__ import annotations

import time
from typing import Any

from .store import Store
from .store_defs import StoreError


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


