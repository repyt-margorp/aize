#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode


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


def run_probe(
    *,
    chrome_bin: str,
    base_url: str,
    password: str,
    session_token: str,
    provider: str,
    timeout_ms: int,
) -> dict:
    query = {"provider": provider}
    if session_token:
        query["session_token"] = session_token
    else:
        query["password"] = password
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
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"chrome_failed rc={completed.returncode} stderr={completed.stderr.strip()}"
        )
    result = extract_result_json(completed.stdout)
    result["chrome_bin"] = chrome_bin
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify HTTPBridge UI markers and basic post-restart actions via headless Chrome.")
    parser.add_argument("--base-url", default="", help="HTTPBridge base URL; when omitted, resolve from runtime state")
    parser.add_argument("--runtime-root", default=str(Path(os.environ.get("AIZE_RUNTIME_ROOT", Path(__file__).resolve().parents[2] / ".aize-runtime"))), help="Runtime root used when resolving the default base URL")
    parser.add_argument("--password", default="ui-verify-pass", help="Bootstrap password for the temporary verification user")
    parser.add_argument("--session-token", default="", help="Existing bridge_session token to reuse instead of bootstrap/login")
    parser.add_argument("--provider", default="codex", choices=["codex", "claude", "gemini"], help="Preferred provider to verify through the UI probe")
    parser.add_argument("--chrome-bin", default="/usr/bin/google-chrome", help="Chrome/Chromium binary path")
    parser.add_argument("--timeout-ms", type=int, default=8000, help="Virtual time budget for headless Chrome")
    args = parser.parse_args()
    runtime_root = Path(args.runtime_root).expanduser().resolve()
    base_url = args.base_url.rstrip("/") if args.base_url else resolve_base_url(runtime_root)

    result = run_probe(
        chrome_bin=args.chrome_bin,
        base_url=base_url,
        password=args.password,
        session_token=args.session_token.strip(),
        provider=args.provider,
        timeout_ms=max(1000, int(args.timeout_ms)),
    )
    result["base_url"] = base_url
    print(json.dumps(result, ensure_ascii=False))
    return 0 if bool(result.get("ok")) else 1


if __name__ == "__main__":
    sys.exit(main())
