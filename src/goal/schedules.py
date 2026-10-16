from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Callable


class ScheduleResolverError(ValueError):
    pass


ScheduleResolver = Callable[[dict[str, Any], dict[str, Any]], str]


def resolve_next_run_at(
    resolver_name: str,
    unit_parameters: dict[str, Any],
    runtime_parameters: dict[str, Any],
) -> str:
    resolver = SCHEDULE_RESOLVERS.get(str(resolver_name or "").strip())
    if resolver is None:
        raise ScheduleResolverError(f"unknown schedule resolver: {resolver_name}")
    return _format_utc(_parse_utc(resolver(dict(unit_parameters), dict(runtime_parameters))))


def explicit_next_run_at(unit_parameters: dict[str, Any], runtime_parameters: dict[str, Any]) -> str:
    del unit_parameters
    call_parameters = runtime_parameters.get("call_parameters")
    if not isinstance(call_parameters, dict):
        call_parameters = {}
    next_run_at = str(call_parameters.get("next_run_at") or "").strip()
    if not next_run_at:
        raise ScheduleResolverError("explicit resolver requires call parameter next_run_at")
    return next_run_at


def next_interval_boundary(unit_parameters: dict[str, Any], runtime_parameters: dict[str, Any]) -> str:
    interval_seconds = int(unit_parameters.get("interval_seconds") or 0)
    if interval_seconds < 1:
        raise ScheduleResolverError("next_interval_boundary requires positive interval_seconds")
    anchor_name = str(unit_parameters.get("anchor") or "scheduled_for").strip()
    anchor_value = str(runtime_parameters.get(anchor_name) or "").strip()
    if not anchor_value:
        raise ScheduleResolverError(f"runtime parameter is required: {anchor_name}")
    completed_at = str(runtime_parameters.get("completed_at") or "").strip()
    if not completed_at:
        raise ScheduleResolverError("runtime parameter is required: completed_at")
    candidate = _parse_utc(anchor_value)
    completed = _parse_utc(completed_at)
    interval = timedelta(seconds=interval_seconds)
    while candidate <= completed:
        candidate += interval
    return _format_utc(candidate)


def _parse_utc(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as exc:
        raise ScheduleResolverError(f"invalid UTC timestamp: {value}") from exc


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


SCHEDULE_RESOLVERS: dict[str, ScheduleResolver] = {
    "explicit": explicit_next_run_at,
    "next_interval_boundary": next_interval_boundary,
}
