#!/usr/bin/env python3
"""Resume a session whose worker panicked.

The active runtime publishes the canonical HttpBridge port in
``$AIZE_RUNTIME_ROOT/state/services.json`` (the same file ``restart_aize_unit.sh``
and ``probe_restart.py`` read). This script discovers that port, mints a
``bridge_session`` token directly from the persistent auth state so it does not
require an interactive admin password, and POSTs ``/session/goal/state`` for the
target session. That handler reuses ``enqueue_goal_dispatch`` to lease a worker
and append a goal-update pending input to the session FIFO, which is the
minimal operation needed to make a panicked session emit a fresh turn while
respecting ``session_permissions`` (``send_prompt`` / ``update_goal`` may be
disabled, but ``/session/goal/state`` with no goal flags is allowed).

Usage:
    python3 scripts/diagnostics/resume_panicked_session.py --session-id <id>

Environment overrides match the rest of the diagnostics scripts:
    AIZE_ROOT, AIZE_RUNTIME_ROOT, AIZE_HTTP_HOST, AIZE_HTTP_PORT, AIZE_TLS,
    AIZE_RESUME_USERNAME (defaults to the session owner discovered from disk).
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    env = os.environ.get("AIZE_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


def _runtime_root(repo_root: Path) -> Path:
    env = os.environ.get("AIZE_RUNTIME_ROOT")
    if env:
        return Path(env).resolve()
    return (repo_root / ".aize-runtime").resolve()


def _state_dir(repo_root: Path, runtime_root: Path) -> Path:
    if runtime_root.name.startswith(".aize-runtime"):
        return runtime_root.parent / ".aize-state"
    return runtime_root / ".aize-state"


def _resolve_http_port(runtime_root: Path) -> str:
    configured = str(os.environ.get("AIZE_HTTP_PORT") or "").strip()
    if configured:
        return configured
    for candidate in (
        runtime_root / "state" / "services.json",
        runtime_root / "manifest.json",
    ):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        services = data.get("services", {})
        if isinstance(services, dict):
            service = services.get("service-http-001")
        elif isinstance(services, list):
            service = next(
                (
                    item
                    for item in services
                    if isinstance(item, dict)
                    and str(item.get("service_id") or item.get("id") or "").strip()
                    == "service-http-001"
                ),
                None,
            )
        else:
            service = None
        port = (
            ((service or {}).get("config") or {}).get("port")
            if isinstance(service, dict)
            else None
        )
        if isinstance(port, int):
            return str(port)
        if isinstance(port, str) and port.strip():
            return port.strip()
    return "4123"


def _resolve_http_host() -> str:
    host = str(os.environ.get("AIZE_HTTP_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return host


def _resolve_scheme() -> str:
    raw = str(os.environ.get("AIZE_TLS", "true")).strip().lower()
    return "http" if raw in {"0", "false", "no", "off"} else "https"


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _find_session_owner(state_dir: Path, session_id: str) -> tuple[str, Path] | None:
    sessions_root = state_dir / "sessions"
    if not sessions_root.exists():
        return None
    for owner_dir in sorted(sessions_root.iterdir()):
        if not owner_dir.is_dir():
            continue
        candidate = owner_dir / session_id
        if candidate.is_dir() and (candidate / "session.json").exists():
            return owner_dir.name, candidate
    return None


def _list_dangling_dispatches(session_dir: Path) -> list[str]:
    pending = session_dir / "pending" / "services"
    if not pending.exists():
        return []
    return sorted(
        entry.name
        for entry in pending.iterdir()
        if entry.is_file() and not entry.name.startswith(".")
    )


def _mint_bridge_session_token(runtime_root: Path, *, username: str) -> str:
    repo_root = _repo_root()
    src_path = repo_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))
    from runtime.persistent_state_pkg.auth import (  # noqa: E402  (deferred import)
        create_session,
    )

    return create_session(runtime_root, username=username)


def _tail_jsonl(path: Path, *, max_records: int = 5) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    tail: list[dict[str, Any]] = []
    for line in raw_lines[-max_records:]:
        line = line.strip()
        if not line:
            continue
        try:
            tail.append(json.loads(line))
        except json.JSONDecodeError:
            tail.append({"raw": line})
    return tail


def _summarize_event(entry: dict[str, Any]) -> dict[str, str]:
    return {
        "ts": str(entry.get("ts") or ""),
        "direction": str(entry.get("direction") or ""),
        "event_type": str(entry.get("event_type") or entry.get("kind") or ""),
        "service_id": str(entry.get("service_id") or ""),
    }


def _post_goal_state_dispatch(
    *,
    base_url: str,
    token: str,
    session_id: str,
    tls_context: ssl.SSLContext,
    timeout: float = 10.0,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}/session/goal/state",
        data=json.dumps({"session_id": session_id}).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Cookie": f"bridge_session={token}",
        },
    )
    open_kwargs: dict[str, Any] = {"timeout": timeout}
    if base_url.startswith("https://"):
        open_kwargs["context"] = tls_context
    try:
        with urllib.request.urlopen(request, **open_kwargs) as response:
            status = int(response.status)
            body_bytes = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        body_bytes = exc.read()
    body_text = body_bytes.decode("utf-8", errors="replace")
    try:
        body_json: Any = json.loads(body_text) if body_text else None
    except json.JSONDecodeError:
        body_json = None
    return {"status": status, "body_text": body_text, "body_json": body_json}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--session-id",
        required=True,
        help="Target session_id (the panicked parent session).",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("AIZE_RESUME_USERNAME") or "",
        help="HttpBridge user used to authenticate (defaults to the session owner on disk).",
    )
    parser.add_argument(
        "--observe-seconds",
        type=float,
        default=8.0,
        help="Seconds to wait after dispatch before tailing the timeline.",
    )
    args = parser.parse_args()

    session_id = args.session_id.strip()
    if not session_id:
        print("session-id is required", file=sys.stderr)
        return 2

    repo_root = _repo_root()
    runtime_root = _runtime_root(repo_root)
    state_dir = _state_dir(repo_root, runtime_root)

    owner_info = _find_session_owner(state_dir, session_id)
    if not owner_info:
        print(
            json.dumps(
                {"ok": False, "error": "session_not_found_on_disk", "session_id": session_id},
                ensure_ascii=False,
            )
        )
        return 1
    discovered_owner, session_dir = owner_info
    username = args.username.strip() or discovered_owner

    timeline_path = session_dir / "timeline.jsonl"
    journal_path = session_dir / "runtime_journal.jsonl"

    before_timeline = _tail_jsonl(timeline_path, max_records=3)
    before_journal = _tail_jsonl(journal_path, max_records=3)
    dangling = _list_dangling_dispatches(session_dir)

    host = _resolve_http_host()
    port = _resolve_http_port(runtime_root)
    scheme = _resolve_scheme()
    base_url = f"{scheme}://{host}:{port}"
    health_url = f"{base_url}/health"
    tls_ctx = _tls_context()

    # Probe health so the report shows whether HttpBridge is actually reachable.
    health_ok = False
    health_body: str | dict[str, Any] = ""
    try:
        open_kwargs: dict[str, Any] = {"timeout": 3}
        if scheme == "https":
            open_kwargs["context"] = tls_ctx
        with urllib.request.urlopen(health_url, **open_kwargs) as response:
            health_ok = response.status == 200
            health_payload = response.read().decode("utf-8", errors="replace")
            try:
                health_body = json.loads(health_payload)
            except json.JSONDecodeError:
                health_body = health_payload
    except (urllib.error.URLError, TimeoutError) as exc:
        health_body = f"unreachable: {exc}"

    token = _mint_bridge_session_token(runtime_root, username=username)
    dispatch_response = _post_goal_state_dispatch(
        base_url=base_url,
        token=token,
        session_id=session_id,
        tls_context=tls_ctx,
    )

    if args.observe_seconds > 0:
        time.sleep(args.observe_seconds)

    after_timeline = _tail_jsonl(timeline_path, max_records=8)
    after_journal = _tail_jsonl(journal_path, max_records=8)

    before_ts = {str(e.get("ts") or "") for e in before_timeline}
    new_timeline_entries = [
        _summarize_event(entry)
        for entry in after_timeline
        if str(entry.get("ts") or "") not in before_ts
    ]
    before_journal_ts = {str(e.get("ts") or "") for e in before_journal}
    new_journal_entries = [
        {
            "ts": str(entry.get("ts") or ""),
            "event_type": str(entry.get("event_type") or ""),
        }
        for entry in after_journal
        if str(entry.get("ts") or "") not in before_journal_ts
    ]

    report = {
        "ok": bool(new_timeline_entries) and 200 <= dispatch_response["status"] < 300,
        "session_id": session_id,
        "username_used": username,
        "discovered_owner": discovered_owner,
        "health_url": health_url,
        "health_ok": health_ok,
        "health_body": health_body,
        "dangling_pending_dispatches": dangling,
        "dispatch": {
            "endpoint": "/session/goal/state",
            "request_payload": {"session_id": session_id},
            "status": dispatch_response["status"],
            "response_json": dispatch_response["body_json"],
        },
        "timeline_tail_before": [_summarize_event(e) for e in before_timeline],
        "timeline_tail_after": [_summarize_event(e) for e in after_timeline],
        "new_timeline_entries": new_timeline_entries,
        "new_runtime_journal_entries": new_journal_entries,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
