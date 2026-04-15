#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from urllib.parse import urlencode


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
    parser.add_argument("--base-url", default="https://127.0.0.1:4123", help="HTTPBridge base URL")
    parser.add_argument("--password", default="ui-verify-pass", help="Bootstrap password for the temporary verification user")
    parser.add_argument("--session-token", default="", help="Existing bridge_session token to reuse instead of bootstrap/login")
    parser.add_argument("--provider", default="codex", choices=["codex", "claude", "gemini"], help="Preferred provider to verify through the UI probe")
    parser.add_argument("--chrome-bin", default="/usr/bin/google-chrome", help="Chrome/Chromium binary path")
    parser.add_argument("--timeout-ms", type=int, default=8000, help="Virtual time budget for headless Chrome")
    args = parser.parse_args()

    result = run_probe(
        chrome_bin=args.chrome_bin,
        base_url=args.base_url.rstrip("/"),
        password=args.password,
        session_token=args.session_token.strip(),
        provider=args.provider,
        timeout_ms=max(1000, int(args.timeout_ms)),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if bool(result.get("ok")) else 1


if __name__ == "__main__":
    sys.exit(main())
