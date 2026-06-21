#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from runtime.persistent_state_pkg import (
    create_conversation_session,
    get_session_settings,
    list_sessions,
    select_session,
    state_path,
)
from runtime.persistent_state_pkg.auth import create_session

TARGET_GOAL_TEXT = "Verify HTTPBridge goal save flow updated"
DEFAULT_HTTP_PORT = "4123"
# Post-restart authenticated root renders can briefly exceed 15s while the
# active bridge rebuilds session summaries and overview state.
DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
DEFAULT_HTML_SNIFF_BYTES = 262144
DEFAULT_HTTP_READ_CHUNK_BYTES = 8192


def _fetch_health_payload(base_url: str) -> dict[str, object]:
    try:
        request = Request(f"{base_url.rstrip('/')}/health", method="GET")
        with urlopen(request, context=_ssl_context(), timeout=DEFAULT_HTTP_TIMEOUT_SECONDS) as response:
            if int(response.status) != 200:
                return {}
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _is_reachable_base_url(base_url: str) -> bool:
    return bool(_fetch_health_payload(base_url))


def _service_http_port_from_state(data: object, *, require_running: bool) -> str:
    if not isinstance(data, dict):
        return ""
    services = data.get("services", {})
    if isinstance(services, dict):
        service = services.get("service-http-001")
    elif isinstance(services, list):
        service = next(
            (
                item
                for item in services
                if isinstance(item, dict)
                and str(item.get("service_id") or item.get("id") or "").strip() == "service-http-001"
            ),
            None,
        )
    else:
        service = None
    if not isinstance(service, dict):
        return ""
    status = str(service.get("status") or "").strip().lower()
    if require_running and status != "running":
        return ""
    return str(((service.get("config") or {}).get("port")) or "").strip()


def _service_http_process_id_from_state(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    services = data.get("services", {})
    if isinstance(services, dict):
        service = services.get("service-http-001")
    elif isinstance(services, list):
        service = next(
            (
                item
                for item in services
                if isinstance(item, dict)
                and str(item.get("service_id") or item.get("id") or "").strip() == "service-http-001"
            ),
            None,
        )
    else:
        service = None
    if not isinstance(service, dict):
        return ""
    return str(service.get("current_process_id") or "").strip()


def _resolve_http_host_and_scheme() -> tuple[str, str]:
    host = str(os.environ.get("AIZE_HTTP_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    tls_raw = str(os.environ.get("AIZE_TLS", "true")).strip().lower()
    scheme = "http" if tls_raw in {"0", "false", "no", "off"} else "https"
    return host, scheme


def _candidate_ports_for_runtime(runtime_root: Path) -> list[str]:
    ports: list[str] = []

    def add_port(value: object) -> None:
        candidate = str(value or "").strip()
        if candidate and candidate not in ports:
            ports.append(candidate)

    state_path = runtime_root / "state" / "services.json"
    try:
        state_data = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state_data = None
    manifest_path = runtime_root / "manifest.json"
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        manifest_data = None

    def raw_service_port(data: object) -> str:
        if not isinstance(data, dict):
            return ""
        services = data.get("services", {})
        if isinstance(services, dict):
            service = services.get("service-http-001")
        elif isinstance(services, list):
            service = next(
                (
                    item
                    for item in services
                    if isinstance(item, dict)
                    and str(item.get("service_id") or item.get("id") or "").strip() == "service-http-001"
                ),
                None,
            )
        else:
            service = None
        if not isinstance(service, dict):
            return ""
        return str(((service.get("config") or {}).get("port")) or "").strip()

    add_port(_service_http_port_from_state(state_data, require_running=True))
    add_port(str(os.environ.get("AIZE_HTTP_PORT") or "").strip())
    add_port(raw_service_port(state_data))
    add_port(raw_service_port(manifest_data))
    add_port(DEFAULT_HTTP_PORT)
    return ports


def resolve_base_url(runtime_root: Path) -> str:
    configured = str(os.environ.get("AIZE_HTTP_BASE_URL") or "").strip()
    if configured:
        configured = configured.rstrip("/")
        if _is_reachable_base_url(configured):
            return configured
    host, scheme = _resolve_http_host_and_scheme()
    state_path = runtime_root / "state" / "services.json"
    try:
        state_data = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state_data = None
    state_running_port = _service_http_port_from_state(state_data, require_running=True)
    state_recorded_port = _service_http_port_from_state(state_data, require_running=False)
    state_process_id = _service_http_process_id_from_state(state_data)
    env_port = str(os.environ.get("AIZE_HTTP_PORT") or "").strip()
    preferred_port = state_running_port or env_port or state_recorded_port or DEFAULT_HTTP_PORT
    preferred_candidate = f"{scheme}://{host}:{preferred_port}"
    if state_running_port:
        running_payload = _fetch_health_payload(preferred_candidate)
        running_ok = bool(running_payload and bool(running_payload.get("ok", True)))
        running_process_id = str((running_payload or {}).get("process_id") or "").strip()
        if running_ok and (
            not state_process_id
            or not running_process_id
            or running_process_id == state_process_id
        ):
            return preferred_candidate
        return preferred_candidate
    if env_port or state_recorded_port:
        return preferred_candidate
    return f"{scheme}://{host}:{DEFAULT_HTTP_PORT}"


def resolve_base_url_candidates(runtime_root: Path) -> list[str]:
    configured = str(os.environ.get("AIZE_HTTP_BASE_URL") or "").strip().rstrip("/")
    if configured:
        return [configured]
    host, scheme = _resolve_http_host_and_scheme()
    primary = resolve_base_url(runtime_root)
    state_path = runtime_root / "state" / "services.json"
    try:
        state_data = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state_data = None
    state_running_port = _service_http_port_from_state(state_data, require_running=True)
    state_process_id = _service_http_process_id_from_state(state_data)
    candidates: list[str] = []
    active_runtime_candidate = ""
    default_candidate = f"{scheme}://{host}:{DEFAULT_HTTP_PORT}"
    if state_running_port:
        runtime_candidate = f"{scheme}://{host}:{state_running_port}"
        runtime_payload = _fetch_health_payload(runtime_candidate)
        if (
            runtime_payload
            and bool(runtime_payload.get("ok", True))
            and (
                not state_process_id
                or not str(runtime_payload.get("process_id") or "").strip()
                or str(runtime_payload.get("process_id") or "").strip() == state_process_id
            )
        ):
            active_runtime_candidate = runtime_candidate
    default_is_stale = False
    if state_running_port and state_process_id:
        default_payload = _fetch_health_payload(default_candidate)
        if default_payload:
            default_process_id = str(default_payload.get("process_id") or "").strip()
            if default_process_id and default_process_id != state_process_id:
                default_is_stale = True
    for candidate in (active_runtime_candidate, primary, default_candidate):
        normalized = str(candidate or "").rstrip("/")
        if not normalized or normalized in candidates:
            continue
        if default_is_stale and normalized == default_candidate:
            continue
        candidates.append(normalized)
    return candidates


def should_retry_direct_verification_with_alternate_base_url(result: dict) -> bool:
    if bool(result.get("ok")):
        return False
    root_status = int(result.get("root_status") or 0)
    session_create_status = int(result.get("session_create_status") or 0)
    created_session_id = str(result.get("created_session_id") or "").strip()
    return (
        root_status in {0, 598, 599}
        or (session_create_status == 401 and not created_session_id)
    )


def extract_result_json(dom_text: str) -> dict:
    match = re.search(r"""<pre\s+id=(["'])result\1>([^<]+)</pre>""", dom_text)
    if not match:
        raise RuntimeError("result_block_missing")
    try:
        return json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"result_json_invalid: {exc}") from exc


def has_ui_markers(html_text: str) -> dict[str, bool]:
    return {
        "session_map": ('id="session-map-pane"' in html_text) or ("id='session-map-pane'" in html_text),
        "workspace_history": (
            (('id="messages"' in html_text) or ("id='messages'" in html_text))
            and (('id="workspace-view"' in html_text) or ("id='workspace-view'" in html_text))
        ),
        "nodes": ('id="nodes-pane"' in html_text) or ("id='nodes-pane'" in html_text),
        "requests": ('id="requests-pane"' in html_text) or ("id='requests-pane'" in html_text),
        "goal_editor": ('id="view-goal"' in html_text) or ("id='view-goal'" in html_text),
    }


def _ssl_context() -> ssl.SSLContext:
    return ssl._create_unverified_context()


def _extract_cookie(header: str, name: str) -> str:
    prefix = f"{name}="
    for part in (header or "").split(";"):
        item = part.strip()
        if item.startswith(prefix):
            return item[len(prefix) :].strip()
    return ""


def http_request(
    *,
    base_url: str,
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    session_token: str = "",
    max_bytes: int | None = None,
) -> tuple[int, str, dict, str]:
    def _read_limited_text(response, limit: int) -> str:
        chunks: list[bytes] = []
        remaining = max(0, int(limit))
        while remaining > 0:
            chunk = response.read(min(DEFAULT_HTTP_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    headers: dict[str, str] = {}
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    if session_token:
        request.add_unredirected_header("Cookie", f"bridge_session={session_token}")
    try:
        with urlopen(request, context=_ssl_context(), timeout=DEFAULT_HTTP_TIMEOUT_SECONDS) as response:
            if max_bytes is not None and max_bytes > 0:
                text = _read_limited_text(response, max_bytes)
            else:
                text = response.read().decode("utf-8")
            parsed = json.loads(text) if text and response.headers.get_content_type() == "application/json" else {}
            return response.status, text, parsed, _extract_cookie(str(response.headers.get("Set-Cookie") or ""), "bridge_session")
    except HTTPError as exc:
        text = exc.read().decode("utf-8")
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {}
        return exc.code, text, parsed, _extract_cookie(str(exc.headers.get("Set-Cookie") or ""), "bridge_session")
    except TimeoutError:
        return 598, "", {"error": "http_timeout"}, ""
    except OSError as exc:
        return 599, "", {"error": f"http_error:{exc}"}, ""


def authenticate_http_session(
    *,
    base_url: str,
    password: str,
    username: str,
) -> tuple[str, str]:
    bootstrap_status, _bootstrap_raw, bootstrap_payload, bootstrap_token = http_request(
        base_url=base_url,
        path="/bootstrap",
        method="POST",
        payload={"password": password},
    )
    if bootstrap_status == 201 and bootstrap_token:
        return bootstrap_token, "bootstrap"
    if bootstrap_status == 400 and str(bootstrap_payload.get("error") or "") == "bootstrap_already_completed":
        login_status, _login_raw, _login_payload, login_token = http_request(
            base_url=base_url,
            path="/login",
            method="POST",
            payload={"username": username, "password": password},
        )
        if login_status == 200 and login_token:
            return login_token, "login"
    return "", ""


def select_http_session(
    *,
    base_url: str,
    session_id: str,
    session_token: str,
) -> tuple[int, str, dict, str]:
    if not session_id.strip() or not session_token.strip():
        return 0, "", {}, ""
    return http_request(
        base_url=base_url,
        path="/session/select",
        method="POST",
        payload={"session_id": session_id},
        session_token=session_token,
    )


def persisted_session_goal_text(runtime_root: Path, *, username: str, session_id: str) -> str:
    session_path = runtime_root.parent / ".aize-state" / "sessions" / username / session_id / "session.json"
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    return str(data.get("goal_text") or "")


def resolve_probe_parent_session_id(
    runtime_root: Path,
    *,
    username: str,
    preferred_session_id: str = "",
) -> str:
    def _is_placeholder_session_id(value: object) -> bool:
        return str(value or "").strip().lower() in {"", "default"}

    def _session_write_capable(session: dict) -> bool:
        permissions = session.get("session_permissions") if isinstance(session, dict) else {}
        if not isinstance(permissions, dict):
            return False
        return (
            bool(permissions.get("create_child_session"))
            and bool(permissions.get("update_goal"))
            and bool(permissions.get("send_prompt"))
        )

    def _session_parent_capable(session: dict) -> bool:
        permissions = session.get("session_permissions") if isinstance(session, dict) else {}
        if not isinstance(permissions, dict):
            return False
        return bool(permissions.get("create_child_session"))

    def _session_can_parent_probe(session: dict) -> bool:
        permissions = session.get("session_permissions") if isinstance(session, dict) else {}
        if not isinstance(permissions, dict):
            return False
        label = str(session.get("label") or "").strip().lower()
        parent_session_id = str(session.get("parent_session_id") or "").strip()
        return bool(permissions.get("create_child_session")) and not parent_session_id and label != "ui verify child"

    def _session_score(session: dict) -> tuple[int, str]:
        permissions = session.get("session_permissions") if isinstance(session, dict) else {}
        if not isinstance(permissions, dict):
            permissions = {}
        if _is_placeholder_session_id(session.get("session_id")):
            return 0, ""
        label = str(session.get("label") or "").strip().lower()
        parent_session_id = str(session.get("parent_session_id") or "").strip()
        score = 0
        if bool(permissions.get("create_child_session")):
            score += 8
        if bool(permissions.get("update_goal")):
            score += 4
        if bool(permissions.get("send_prompt")):
            score += 4
        if str(session.get("created_by_type") or "").strip().lower() == "user":
            score += 2
        if not parent_session_id:
            score += 6
        if label == "root":
            score += 3
        if label == "ui verify child":
            score -= 10
        updated_at = str(session.get("updated_at") or session.get("created_at") or "")
        return score, updated_at

    preferred = preferred_session_id.strip()
    if preferred:
        candidate = get_session_settings(runtime_root, username=username, session_id=preferred) or {}
        if _session_score(candidate)[0] >= 8 and (_session_write_capable(candidate) or _session_parent_capable(candidate)):
            return preferred

    sessions_dir = runtime_root.parent / ".aize-state" / "sessions" / username
    local_sessions: list[dict] = []
    if sessions_dir.exists():
        for session_dir in sorted(path for path in sessions_dir.iterdir() if path.is_dir()):
            session_path = session_dir / "session.json"
            try:
                stored = json.loads(session_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                continue
            if isinstance(stored, dict):
                local_sessions.append(stored)
    if local_sessions:
        ranked_local = sorted(
            (
                (session, *_session_score(session))
                for session in local_sessions
                if isinstance(session, dict) and not _is_placeholder_session_id(session.get("session_id"))
            ),
            key=lambda item: (item[1], item[2], str(item[0].get("session_id") or "")),
            reverse=True,
        )
        for session, score, _updated_at in ranked_local:
            if score >= 8 and _session_can_parent_probe(session):
                return str(session.get("session_id") or "").strip()
        for session, score, _updated_at in ranked_local:
            if score >= 8 and _session_write_capable(session):
                return str(session.get("session_id") or "").strip()

    sessions = list_sessions(runtime_root, username=username)
    ranked = sorted(
        (
            (session, *_session_score(session))
            for session in sessions
            if isinstance(session, dict) and not _is_placeholder_session_id(session.get("session_id"))
        ),
        key=lambda item: (item[1], item[2], str(item[0].get("session_id") or "")),
        reverse=True,
    )
    for session, score, _updated_at in ranked:
        if score >= 8 and _session_can_parent_probe(session):
            return str(session.get("session_id") or "").strip()
    for session, score, _updated_at in ranked:
        if score >= 8 and _session_write_capable(session):
            return str(session.get("session_id") or "").strip()
    bootstrap = create_conversation_session(
        runtime_root,
        username=username,
        label="UI Verify Parent",
        created_by_username=username,
        created_by_type="user",
    )
    return str((bootstrap or {}).get("session_id") or "").strip()


def run_probe(
    *,
    chrome_bin: str,
    base_url: str,
    password: str,
    session_token: str,
    parent_session_id: str,
    provider: str,
    timeout_ms: int,
) -> dict:
    query = {"provider": provider}
    if session_token:
        query["session_token"] = session_token
    else:
        query["password"] = password
    if parent_session_id:
        query["parent_session_id"] = parent_session_id
    probe_url = f"{base_url.rstrip('/')}/diagnostics/ui-probe?{urlencode(query)}"
    command = [
        chrome_bin,
        "--headless=new",
        "--disable-gpu",
        "--ignore-certificate-errors",
        f"--virtual-time-budget={timeout_ms}",
        "--dump-dom",
        probe_url,
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=max(5, int(timeout_ms / 1000) + 5),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"chrome_failed rc={completed.returncode} stderr={completed.stderr.strip()}"
        )
    result = extract_result_json(completed.stdout)
    result["chrome_bin"] = chrome_bin
    return result


def safe_run_probe(**kwargs) -> dict:
    try:
        return run_probe(**kwargs)
    except subprocess.TimeoutExpired as exc:
        timeout_seconds = exc.timeout if exc.timeout is not None else "unknown"
        return {
            "ok": False,
            "error": f"chrome_timeout:{timeout_seconds}",
            "provider": str(kwargs.get("provider") or ""),
        }
    except RuntimeError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "provider": str(kwargs.get("provider") or ""),
        }


def run_direct_verification(
    *,
    runtime_root: Path,
    base_url: str,
    session_token: str,
    parent_session_id: str,
    provider: str,
    password: str,
    username: str,
    send_prompt: bool = False,
) -> dict:
    def create_child_session(*, token: str, parent_id: str) -> tuple[int, str, dict, str]:
        payload = {
            "label": "UI Verify Child",
            **({"session_id": parent_id} if parent_id else {}),
            **({"parent_session_id": parent_id} if parent_id else {}),
        }
        return http_request(
            base_url=base_url,
            path="/sessions",
            method="POST",
            payload=payload,
            session_token=token,
        )

    def _root_probe_usable(*, status: int, html_text: str, payload: dict) -> bool:
        if status != 200:
            return False
        markers = has_ui_markers(html_text)
        if markers["session_map"] and markers["workspace_history"]:
            return True
        lower_html = html_text.lower()
        if "<title>httpbridge login</title>" in lower_html or "<html>login</html>" in lower_html:
            return False
        return bool(html_text.strip()) and str(payload.get("error") or "") == ""

    active_session_token = session_token.strip()
    initial_session_token_supplied = bool(active_session_token)
    effective_parent_session_id = parent_session_id
    auth_mode = "session_token" if initial_session_token_supplied else ""
    select_status, _select_raw, _select_payload, _select_cookie = (0, "", {}, "")
    root_path = (
        f"/?{urlencode({'session_id': effective_parent_session_id})}"
        if effective_parent_session_id
        else "/"
    )
    root_status, root_html, root_payload, _root_cookie = http_request(
        base_url=base_url,
        path=root_path,
        session_token=active_session_token,
        max_bytes=DEFAULT_HTML_SNIFF_BYTES,
    )
    initial_root_markers = has_ui_markers(root_html)
    root_looks_like_login_shell = (
        "<title>HttpBridge Login</title>" in root_html
        or "<html>login</html>" in root_html.lower()
    )
    should_refresh_root_auth = (
        root_status in {401, 403}
        or (not initial_session_token_supplied and root_looks_like_login_shell)
        or (initial_session_token_supplied and root_looks_like_login_shell)
    )

    if should_refresh_root_auth:
        replacement_session_token, replacement_auth_mode = authenticate_http_session(
            base_url=base_url,
            password=password,
            username=username,
        )
        if replacement_session_token:
            refreshed_root_status, refreshed_root_html, refreshed_root_payload, refreshed_root_cookie = http_request(
                base_url=base_url,
                path=root_path,
                session_token=replacement_session_token,
                max_bytes=DEFAULT_HTML_SNIFF_BYTES,
            )
            active_session_token = replacement_session_token
            root_status = refreshed_root_status
            root_html = refreshed_root_html
            root_payload = refreshed_root_payload
            _root_cookie = refreshed_root_cookie
            if replacement_auth_mode:
                auth_mode = replacement_auth_mode

    child_session_payload = {
        "label": "UI Verify Child",
        **({"session_id": effective_parent_session_id} if effective_parent_session_id else {}),
        **({"parent_session_id": effective_parent_session_id} if effective_parent_session_id else {}),
    }
    fallback_root_status, fallback_root_html, fallback_root_payload = root_status, root_html, root_payload
    child_status, _child_raw, child_payload, _child_cookie = create_child_session(
        token=active_session_token,
        parent_id=effective_parent_session_id,
    )
    attempted_child_auth_refresh = False
    if (
        effective_parent_session_id
        and child_status in {401, 404}
        and str(child_payload.get("error") or "") in {"auth_required_or_invalid_talk", "parent_session_not_found"}
    ):
        fallback_root_status, fallback_root_html, fallback_root_payload, _fallback_root_cookie = http_request(
            base_url=base_url,
            path="/",
            session_token=active_session_token,
            max_bytes=DEFAULT_HTML_SNIFF_BYTES,
        )
        replacement_session_token, replacement_auth_mode = authenticate_http_session(
            base_url=base_url,
            password=password,
            username=username,
        )
        attempted_child_auth_refresh = True
        if replacement_session_token:
            active_session_token = replacement_session_token
            root_status, root_html, root_payload, _root_cookie = http_request(
                base_url=base_url,
                path="/",
                session_token=active_session_token,
                max_bytes=DEFAULT_HTML_SNIFF_BYTES,
            )
            child_status, _child_raw, child_payload, _child_cookie = create_child_session(
                token=active_session_token,
                parent_id=effective_parent_session_id,
            )
        else:
            replacement_session_token = maybe_mint_local_session_token_if_available(
                runtime_root,
                username=username,
                active_session_id=(
                    effective_parent_session_id
                    if _root_probe_usable(
                        status=fallback_root_status,
                        html_text=fallback_root_html,
                        payload=fallback_root_payload,
                    )
                    else ""
                ),
            )
            if replacement_session_token:
                active_session_token = replacement_session_token
                child_status, _child_raw, child_payload, _child_cookie = create_child_session(
                    token=active_session_token,
                    parent_id=effective_parent_session_id,
                )
        if child_status in {401, 404} and str(child_payload.get("error") or "") in {
            "auth_required_or_invalid_talk",
            "parent_session_not_found",
        }:
            child_session_payload = {"label": "UI Verify Child"}
            child_status, _child_raw, child_payload, _child_cookie = create_child_session(
                token=active_session_token,
                parent_id="",
            )
    if child_status == 401:
        replacement_session_token = ""
        replacement_auth_mode = ""
        if not attempted_child_auth_refresh:
            replacement_session_token, replacement_auth_mode = authenticate_http_session(
                base_url=base_url,
                password=password,
                username=username,
            )
        if not replacement_session_token:
            replacement_session_token, replacement_auth_mode = authenticate_http_session(
                base_url=base_url,
                password=password,
                username=username,
            )
        if not replacement_session_token:
            replacement_session_token = maybe_mint_local_session_token_if_available(
                runtime_root,
                username=username,
                active_session_id="",
            )
            if replacement_session_token:
                replacement_auth_mode = "local_session_token"
        if replacement_session_token:
            active_session_token = replacement_session_token
            if replacement_auth_mode and not initial_session_token_supplied:
                auth_mode = replacement_auth_mode
            elif replacement_auth_mode == "local_session_token":
                auth_mode = replacement_auth_mode
            root_status, root_html, root_payload, _root_cookie = http_request(
                base_url=base_url,
                path="/",
                session_token=active_session_token,
                max_bytes=DEFAULT_HTML_SNIFF_BYTES,
            )
            if effective_parent_session_id:
                select_status, _select_raw, _select_payload, _select_cookie = select_http_session(
                    base_url=base_url,
                    session_id=effective_parent_session_id,
                    session_token=active_session_token,
                )
            child_status, _child_raw, child_payload, _child_cookie = create_child_session(
                token=active_session_token,
                parent_id=effective_parent_session_id,
            )

    session_id = str(child_payload.get("active_session_id") or ((child_payload.get("session") or {}).get("session_id")) or "").strip()
    goal_update_status, _goal_update_raw, goal_update_payload, _goal_cookie = (0, "", {}, "")
    provider_update_status, _provider_update_raw, provider_update_payload, _provider_cookie = (0, "", {}, "")
    prompt_send_status, _prompt_send_raw, prompt_send_payload, _prompt_cookie = (0, "", {}, "")
    child_page_status, child_html, _child_page_payload, _child_page_cookie = (0, "", {}, "")
    if session_id:
        goal_update_status, _goal_update_raw, goal_update_payload, _goal_cookie = http_request(
            base_url=base_url,
            path="/session/goal",
            method="POST",
            payload={"session_id": session_id, "goal_text": TARGET_GOAL_TEXT},
            session_token=active_session_token,
        )
        provider_update_status, _provider_update_raw, provider_update_payload, _provider_cookie = http_request(
            base_url=base_url,
            path="/session/goal/state",
            method="POST",
            payload={"session_id": session_id, "preferred_provider": provider},
            session_token=active_session_token,
        )
        if send_prompt:
            prompt_send_status, _prompt_send_raw, prompt_send_payload, _prompt_cookie = http_request(
                base_url=base_url,
                path="/message",
                method="POST",
                payload={"session_id": session_id, "text": f"UI smoke prompt after restart via {provider}", "provider": provider},
                session_token=active_session_token,
            )
        if _root_probe_usable(status=root_status, html_text=root_html, payload=root_payload):
            child_page_status, child_html, _child_page_payload, _child_page_cookie = http_request(
                base_url=base_url,
                path=f"/?{urlencode({'session_id': session_id})}",
                session_token=active_session_token,
                max_bytes=DEFAULT_HTML_SNIFF_BYTES,
            )

    session_markers = has_ui_markers(root_html)
    child_markers = has_ui_markers(child_html)
    effective_provider = str(provider_update_payload.get("preferred_provider") or provider or "").strip()
    prompt_provider = str(prompt_send_payload.get("provider") or "").strip()
    persisted_goal_text = persisted_session_goal_text(runtime_root, username=username, session_id=session_id) if session_id else ""
    root_probe_usable = _root_probe_usable(status=root_status, html_text=root_html, payload=root_payload)
    prompt_flow_ok = True if not send_prompt else (prompt_send_status == 202 and prompt_provider == provider)
    write_flow_ok = (
        child_status == 201
        and 200 <= goal_update_status < 300
        and provider_update_status == 200
        and effective_provider == provider
        and persisted_goal_text == TARGET_GOAL_TEXT
    )
    full_page_ok = (
        write_flow_ok
        and prompt_flow_ok
        and root_probe_usable
        and child_page_status == 200
        and session_markers["session_map"]
        and session_markers["workspace_history"]
        and child_markers["session_map"]
        and child_markers["workspace_history"]
    )
    goal_save_only_ok = (
        write_flow_ok
        and (root_probe_usable or root_status in {200, 401, 598, 599})
        and child_page_status in {0, 200, 401, 598, 599}
    )
    verification_mode = "http_api_full" if full_page_ok else ("http_api_goal_save_only" if goal_save_only_ok else "http_api")
    return {
        "ok": full_page_ok or goal_save_only_ok,
        "provider": provider,
        "session_markers": session_markers,
        "child_markers": child_markers,
        "created_session_id": session_id,
        "goal_update_status": goal_update_status,
        "provider_update_status": provider_update_status,
        "effective_provider": effective_provider,
        "prompt_send_status": prompt_send_status,
        "prompt_provider": prompt_provider,
        "persisted_goal_text": persisted_goal_text,
        "target_goal_text": TARGET_GOAL_TEXT,
        "root_status": root_status,
        "root_error": str(root_payload.get("error") or ""),
        "session_select_status": select_status,
        "session_select_error": str(_select_payload.get("error") or ""),
        "session_create_status": child_status,
        "session_create_error": str(child_payload.get("error") or ""),
        "goal_update_error": str(goal_update_payload.get("error") or ""),
        "provider_update_error": str(provider_update_payload.get("error") or ""),
        "prompt_send_error": str(prompt_send_payload.get("error") or ""),
        "child_page_status": child_page_status,
        "verification_mode": verification_mode,
        "parent_session_id": parent_session_id,
        "effective_parent_session_id": effective_parent_session_id,
        "parent_session_fallback": bool(parent_session_id and not effective_parent_session_id),
        "auth_mode": auth_mode or "unauthenticated",
        "write_flow_ok": write_flow_ok,
        "prompt_flow_ok": prompt_flow_ok,
        "full_page_ok": full_page_ok,
        "root_probe_usable": root_probe_usable,
    }
def maybe_mint_local_session_token(
    runtime_root: Path,
    *,
    username: str,
    active_session_id: str = "",
) -> str:
    persistent_path = state_path(runtime_root)
    if not persistent_path.exists():
        raise RuntimeError("local_persistent_state_missing")
    token = create_session(runtime_root, username=username)
    if active_session_id:
        select_session(runtime_root, token=token, session_id=active_session_id)
    return token


def maybe_mint_local_session_token_if_available(
    runtime_root: Path,
    *,
    username: str,
    active_session_id: str = "",
) -> str:
    try:
        return maybe_mint_local_session_token(
            runtime_root,
            username=username,
            active_session_id=active_session_id,
        )
    except RuntimeError as exc:
        if str(exc) == "local_persistent_state_missing":
            return ""
        raise


def needs_local_session_fallback(result: dict, *, session_token: str) -> bool:
    error = str(result.get("error") or "")
    if session_token:
        return (
            not bool(result.get("ok"))
            and (
                "login_failed:" in error
                or "bootstrap_failed:" in error
                or "session_create_failed:" in error
                or "parent_session_not_found" in error
                or "auth_required_or_invalid_talk" in error
            )
        )
    if bool(result.get("ok")):
        return False
    return (
        "login_failed:" in error
        or "bootstrap_failed:" in error
        or "chrome_timeout:" in error
        or "session_create_failed:" in error
        or "parent_session_not_found" in error
        or "auth_required_or_invalid_talk" in error
    )


def needs_direct_http_fallback(result: dict, *, session_token: str) -> bool:
    if bool(result.get("ok")):
        return False
    error = str(result.get("error") or "")
    if "chrome_timeout:" in error:
        return True
    if not session_token:
        return False
    return (
        "session_create_failed:" in error
        or "auth_required_or_invalid_session" in error
        or "parent_session_not_found" in error
        or "auth_required_or_invalid_talk" in error
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify HTTPBridge UI markers and basic post-restart actions via headless Chrome.")
    parser.add_argument("--base-url", default="", help="HTTPBridge base URL; when omitted, resolve from runtime state")
    parser.add_argument("--runtime-root", default=str(Path(os.environ.get("AIZE_RUNTIME_ROOT", Path(__file__).resolve().parents[2] / ".aize-runtime"))), help="Runtime root used when resolving the default base URL")
    parser.add_argument("--password", default="ui-verify-pass", help="Bootstrap password for the temporary verification user")
    parser.add_argument("--session-token", default="", help="Existing bridge_session token to reuse instead of bootstrap/login")
    parser.add_argument("--username", default="root", help="Local username to mint a fallback bridge_session token for when password bootstrap/login is not reusable")
    parser.add_argument("--parent-session-id", default="", help="Explicit parent session id to use when creating the temporary verification child session")
    parser.add_argument("--provider", default="codex", choices=["codex", "claude", "gemini"], help="Preferred provider to verify through the UI probe")
    parser.add_argument("--chrome-bin", default="/usr/bin/google-chrome", help="Chrome/Chromium binary path")
    parser.add_argument("--timeout-ms", type=int, default=8000, help="Virtual time budget for headless Chrome")
    parser.add_argument(
        "--send-prompt",
        action="store_true",
        help="Also send a smoke prompt through /message. This starts agent work and is disabled by default.",
    )
    parser.add_argument(
        "--verification-mode",
        default="chrome",
        choices=["http_api", "chrome"],
        help="Verification backend. Use chrome for the headless browser probe or http_api for the direct HTTP flow.",
    )
    args = parser.parse_args()
    runtime_root = Path(args.runtime_root).expanduser().resolve()

    session_token = args.session_token.strip()
    minted_session_token = ""
    requested_parent_session_id = str(args.parent_session_id or "").strip()
    parent_session_id = requested_parent_session_id
    auto_parent_session_id = ""
    resolved_username = str(args.username or "").strip() or "root"
    if not parent_session_id:
        auto_parent_session_id = resolve_probe_parent_session_id(
            runtime_root,
            username=resolved_username,
        )
        parent_session_id = auto_parent_session_id
    timeout_ms = max(1000, int(args.timeout_ms))
    candidate_base_urls = [args.base_url.rstrip("/")] if args.base_url else resolve_base_url_candidates(runtime_root)
    base_url = candidate_base_urls[0] if candidate_base_urls else ""
    result: dict = {}
    active_session_token = session_token
    direct_http_only = bool(requested_parent_session_id) or args.verification_mode == "http_api" or not session_token
    explicit_http_api_mode = args.verification_mode == "http_api"
    for candidate_base_url in candidate_base_urls:
        base_url = candidate_base_url
        session_token_for_base_url = active_session_token

        # Exercise the write path directly when the caller explicitly requests it,
        # when a parent session is pinned, or when there is no reusable browser
        # session yet. This keeps goal-save verification deterministic and avoids
        # headless-browser hangs during restart smoke checks.
        if direct_http_only:
            direct_parent_session_id = requested_parent_session_id or parent_session_id
            direct_session_token = session_token_for_base_url
            should_mint_direct_token = not direct_session_token
            if should_mint_direct_token:
                if not minted_session_token:
                    mint_active_session_id = requested_parent_session_id if requested_parent_session_id else ""
                    minted_session_token = maybe_mint_local_session_token_if_available(
                        runtime_root,
                        username=resolved_username,
                        active_session_id=mint_active_session_id,
                    )
                if minted_session_token:
                    direct_session_token = minted_session_token
                    active_session_token = minted_session_token
            result = run_direct_verification(
                runtime_root=runtime_root,
                base_url=base_url,
                session_token=direct_session_token,
                parent_session_id=direct_parent_session_id,
                provider=args.provider,
                password=args.password,
                username=resolved_username,
                send_prompt=args.send_prompt,
            )
            if (
                not bool(result.get("ok"))
                and not requested_parent_session_id
                and not explicit_http_api_mode
                and direct_parent_session_id == auto_parent_session_id
                and direct_parent_session_id
                and int(result.get("session_create_status") or 0) in {401, 404, 598, 599}
            ):
                retry_direct_session_token = direct_session_token
                if not retry_direct_session_token:
                    retry_direct_session_token = maybe_mint_local_session_token_if_available(
                        runtime_root,
                        username=resolved_username,
                        active_session_id="",
                    )
                result = run_direct_verification(
                    runtime_root=runtime_root,
                    base_url=base_url,
                    session_token=retry_direct_session_token,
                    parent_session_id="",
                    provider=args.provider,
                    password=args.password,
                    username=resolved_username,
                    send_prompt=args.send_prompt,
                )
            if not bool(result.get("ok")) and not should_retry_direct_verification_with_alternate_base_url(result):
                break
        else:
            result = safe_run_probe(
                chrome_bin=args.chrome_bin,
                base_url=base_url,
                password=args.password,
                session_token=session_token_for_base_url,
                parent_session_id=parent_session_id,
                provider=args.provider,
                timeout_ms=timeout_ms,
            )
            if needs_local_session_fallback(result, session_token=session_token_for_base_url):
                parent_session_id = resolve_probe_parent_session_id(
                    runtime_root,
                    username=resolved_username,
                    preferred_session_id=parent_session_id,
                )
                if not minted_session_token:
                    minted_session_token = maybe_mint_local_session_token_if_available(
                        runtime_root,
                        username=resolved_username,
                        active_session_id=requested_parent_session_id,
                    )
                if minted_session_token:
                    session_token_for_base_url = minted_session_token
                    active_session_token = minted_session_token
                    result = safe_run_probe(
                        chrome_bin=args.chrome_bin,
                        base_url=base_url,
                        password=args.password,
                        session_token=session_token_for_base_url,
                        parent_session_id=parent_session_id,
                        provider=args.provider,
                        timeout_ms=timeout_ms,
                    )
                    result["auth_fallback"] = "local_session_token"
                    result["auth_username"] = resolved_username
            if needs_direct_http_fallback(result, session_token=session_token_for_base_url):
                if not minted_session_token:
                    minted_session_token = maybe_mint_local_session_token_if_available(
                        runtime_root,
                        username=resolved_username,
                        active_session_id="",
                    )
                direct_session_token = minted_session_token or session_token_for_base_url
                direct_result = run_direct_verification(
                    runtime_root=runtime_root,
                    base_url=base_url,
                    session_token=direct_session_token,
                    parent_session_id=parent_session_id,
                    provider=args.provider,
                    password=args.password,
                    username=resolved_username,
                    send_prompt=args.send_prompt,
                )
                if (
                    not bool(direct_result.get("ok"))
                    and not requested_parent_session_id
                    and parent_session_id == auto_parent_session_id
                    and parent_session_id
                    and int(direct_result.get("session_create_status") or 0) in {401, 404, 598, 599}
                ):
                    retry_direct_session_token = direct_session_token
                    direct_result = run_direct_verification(
                        runtime_root=runtime_root,
                        base_url=base_url,
                        session_token=retry_direct_session_token,
                        parent_session_id="",
                        provider=args.provider,
                        password=args.password,
                        username=resolved_username,
                        send_prompt=args.send_prompt,
                    )
                for key in ("auth_fallback", "auth_username", "chrome_bin"):
                    if key in result:
                        direct_result[key] = result[key]
                result = direct_result
                if not bool(result.get("ok")) and not should_retry_direct_verification_with_alternate_base_url(result):
                    break
        if bool(result.get("ok")):
            break
    if parent_session_id and "parent_session_id" not in result:
        result["parent_session_id"] = parent_session_id
    result["base_url"] = base_url
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if bool(result.get("ok")) else 1


if __name__ == "__main__":
    sys.exit(main())
