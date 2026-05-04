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

from runtime.persistent_state_pkg import get_session_settings, list_sessions
from runtime.persistent_state_pkg.auth import create_session

TARGET_GOAL_TEXT = "Verify HTTPBridge goal save flow updated"


def resolve_base_url(runtime_root: Path) -> str:
    configured = str(os.environ.get("AIZE_HTTP_BASE_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    host = str(os.environ.get("AIZE_HTTP_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = str(os.environ.get("AIZE_HTTP_PORT") or "").strip()
    if not port:
        for path in (
            runtime_root / "state" / "services.json",
            runtime_root / "manifest.json",
        ):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
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
                        and str(item.get("service_id") or item.get("id") or "").strip() == "service-http-001"
                    ),
                    None,
                )
            else:
                service = None
            value = ((service or {}).get("config") or {}).get("port") if isinstance(service, dict) else None
            if isinstance(value, int):
                port = str(value)
                break
            if isinstance(value, str) and value.strip():
                port = value.strip()
                break
    if not port:
        port = "4123"
    tls_raw = str(os.environ.get("AIZE_TLS", "true")).strip().lower()
    scheme = "http" if tls_raw in {"0", "false", "no", "off"} else "https"
    return f"{scheme}://{host}:{port}"


def extract_result_json(dom_text: str) -> dict:
    match = re.search(r'<pre id="result">([^<]+)</pre>', dom_text)
    if not match:
        raise RuntimeError("result_block_missing")
    try:
        return json.loads(match.group(1))
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


def http_request(
    *,
    base_url: str,
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    session_token: str = "",
) -> tuple[int, str, dict]:
    headers: dict[str, str] = {}
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if session_token:
        headers["Cookie"] = f"bridge_session={session_token}"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, context=_ssl_context()) as response:
            text = response.read().decode("utf-8")
            parsed = json.loads(text) if text and response.headers.get_content_type() == "application/json" else {}
            return response.status, text, parsed
    except HTTPError as exc:
        text = exc.read().decode("utf-8")
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {}
        return exc.code, text, parsed


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
    def _session_score(session: dict) -> tuple[int, str]:
        permissions = session.get("session_permissions") if isinstance(session, dict) else {}
        if not isinstance(permissions, dict):
            permissions = {}
        score = 0
        if bool(permissions.get("create_child_session")):
            score += 8
        if bool(permissions.get("update_goal")):
            score += 4
        if bool(permissions.get("send_prompt")):
            score += 4
        if str(session.get("created_by_type") or "").strip().lower() == "user":
            score += 2
        if str(session.get("label") or "").strip().lower() == "root":
            score += 1
        updated_at = str(session.get("updated_at") or session.get("created_at") or "")
        return score, updated_at

    preferred = preferred_session_id.strip()
    if preferred:
        candidate = get_session_settings(runtime_root, username=username, session_id=preferred) or {}
        if _session_score(candidate)[0] >= 8:
            return preferred

    sessions = list_sessions(runtime_root, username=username)
    ranked = sorted(
        (
            (session, *_session_score(session))
            for session in sessions
            if isinstance(session, dict) and str(session.get("session_id") or "").strip()
        ),
        key=lambda item: (item[1], item[2], str(item[0].get("session_id") or "")),
        reverse=True,
    )
    for session, score, _updated_at in ranked:
        if score >= 8:
            return str(session.get("session_id") or "").strip()
    return preferred


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
    username: str,
) -> dict:
    root_status, root_html, _root_payload = http_request(
        base_url=base_url,
        path="/",
        session_token=session_token,
    )
    child_status, _child_raw, child_payload = http_request(
        base_url=base_url,
        path="/sessions",
        method="POST",
        payload={
            "label": "UI Verify Child",
            **({"parent_session_id": parent_session_id} if parent_session_id else {}),
        },
        session_token=session_token,
    )
    session_id = str(child_payload.get("active_session_id") or ((child_payload.get("session") or {}).get("session_id")) or "").strip()
    goal_update_status, _goal_update_raw, _goal_update_payload = (0, "", {})
    provider_update_status, _provider_update_raw, provider_update_payload = (0, "", {})
    prompt_send_status, _prompt_send_raw, prompt_send_payload = (0, "", {})
    child_page_status, child_html, _child_page_payload = (0, "", {})
    if session_id:
        goal_update_status, _goal_update_raw, _goal_update_payload = http_request(
            base_url=base_url,
            path="/session/goal",
            method="POST",
            payload={"session_id": session_id, "goal_text": TARGET_GOAL_TEXT},
            session_token=session_token,
        )
        provider_update_status, _provider_update_raw, provider_update_payload = http_request(
            base_url=base_url,
            path="/session/goal/state",
            method="POST",
            payload={"session_id": session_id, "preferred_provider": provider},
            session_token=session_token,
        )
        prompt_send_status, _prompt_send_raw, prompt_send_payload = http_request(
            base_url=base_url,
            path="/message",
            method="POST",
            payload={"session_id": session_id, "text": f"UI smoke prompt after restart via {provider}", "provider": provider},
            session_token=session_token,
        )
        child_page_status, child_html, _child_page_payload = http_request(
            base_url=base_url,
            path=f"/?{urlencode({'session_id': session_id})}",
            session_token=session_token,
        )
    session_markers = has_ui_markers(root_html)
    child_markers = has_ui_markers(child_html)
    effective_provider = str(provider_update_payload.get("preferred_provider") or provider or "").strip()
    prompt_provider = str(prompt_send_payload.get("provider") or "").strip()
    persisted_goal_text = persisted_session_goal_text(runtime_root, username=username, session_id=session_id) if session_id else ""
    return {
        "ok": (
            root_status == 200
            and child_status == 201
            and child_page_status == 200
            and session_markers["session_map"]
            and session_markers["workspace_history"]
            and child_markers["session_map"]
            and child_markers["workspace_history"]
            and goal_update_status == 200
            and provider_update_status == 200
            and effective_provider == provider
            and prompt_send_status == 202
            and prompt_provider == provider
            and persisted_goal_text == TARGET_GOAL_TEXT
        ),
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
        "session_create_status": child_status,
        "child_page_status": child_page_status,
        "verification_mode": "http_api",
        "parent_session_id": parent_session_id,
    }


def maybe_mint_local_session_token(runtime_root: Path, *, username: str) -> str:
    persistent_path = runtime_root.parent / ".aize-state" / "persistent.json"
    if not persistent_path.exists():
        raise RuntimeError("local_persistent_state_missing")
    return create_session(runtime_root, username=username)


def needs_local_session_fallback(result: dict, *, session_token: str) -> bool:
    if session_token:
        return False
    if bool(result.get("ok")):
        return False
    error = str(result.get("error") or "")
    return "login_failed:" in error or "bootstrap_failed:" in error


def needs_direct_http_fallback(result: dict, *, session_token: str) -> bool:
    if not session_token:
        return False
    if bool(result.get("ok")):
        return False
    error = str(result.get("error") or "")
    return (
        "chrome_timeout:" in error
        or "session_create_failed:" in error
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
    args = parser.parse_args()
    runtime_root = Path(args.runtime_root).expanduser().resolve()
    base_url = args.base_url.rstrip("/") if args.base_url else resolve_base_url(runtime_root)

    session_token = args.session_token.strip()
    parent_session_id = str(args.parent_session_id or "").strip()
    resolved_username = str(args.username or "").strip() or "root"
    if not parent_session_id:
        parent_session_id = resolve_probe_parent_session_id(
            runtime_root,
            username=resolved_username,
        )
    timeout_ms = max(1000, int(args.timeout_ms))
    result = safe_run_probe(
        chrome_bin=args.chrome_bin,
        base_url=base_url,
        password=args.password,
        session_token=session_token,
        parent_session_id=parent_session_id,
        provider=args.provider,
        timeout_ms=timeout_ms,
    )
    if needs_local_session_fallback(result, session_token=session_token):
        parent_session_id = resolve_probe_parent_session_id(
            runtime_root,
            username=resolved_username,
            preferred_session_id=parent_session_id,
        )
        session_token = maybe_mint_local_session_token(
            runtime_root,
            username=resolved_username,
        )
        result = safe_run_probe(
            chrome_bin=args.chrome_bin,
            base_url=base_url,
            password=args.password,
            session_token=session_token,
            parent_session_id=parent_session_id,
            provider=args.provider,
            timeout_ms=timeout_ms,
        )
        result["auth_fallback"] = "local_session_token"
        result["auth_username"] = resolved_username
    if needs_direct_http_fallback(result, session_token=session_token):
        direct_result = run_direct_verification(
            runtime_root=runtime_root,
            base_url=base_url,
            session_token=session_token,
            parent_session_id=parent_session_id,
            provider=args.provider,
            username=resolved_username,
        )
        for key in ("auth_fallback", "auth_username", "chrome_bin"):
            if key in result:
                direct_result[key] = result[key]
        result = direct_result
    if parent_session_id and "parent_session_id" not in result:
        result["parent_session_id"] = parent_session_id
    result["base_url"] = base_url
    print(json.dumps(result, ensure_ascii=False))
    return 0 if bool(result.get("ok")) else 1


if __name__ == "__main__":
    sys.exit(main())
