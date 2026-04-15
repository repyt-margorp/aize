#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
import re


ROOT = Path(os.environ.get("AIZE_ROOT", Path(__file__).resolve().parents[2]))
RUNTIME_ROOT = Path(os.environ.get("AIZE_RUNTIME_ROOT", str(ROOT / ".agent-mesh-runtime")))
HTTP_HOST = os.environ.get("AIZE_HTTP_HOST", "127.0.0.1")
HTTP_PORT = os.environ.get("AIZE_HTTP_PORT", "4123")
NODE_ID = os.environ.get("AIZE_NODE_ID") or f"node-{re.sub(r'[^a-z0-9]+', '-', ROOT.name.lower()).strip('-') or 'local'}"
RESTART_SCRIPT = ROOT / "restart_codex_http_mesh.sh"
LOG_DIR = ROOT / ".temp" / "restart-debug" / "logs"
HEALTH_URL = f"https://{HTTP_HOST}:{HTTP_PORT}/health"
_TLS_CTX = ssl.create_default_context()
_TLS_CTX.check_hostname = False
_TLS_CTX.verify_mode = ssl.CERT_NONE
PROCESS_PATTERNS = {
    "parent": f"cli.run_codex_http_mesh --runtime-root {RUNTIME_ROOT}",
    "router": f"python3 -m kernel.router --manifest {RUNTIME_ROOT / 'manifest.json'}",
    "adapter": f"python3 -m runtime.cli_service_adapter --manifest {RUNTIME_ROOT / 'manifest.json'}",
}


def sh(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, encoding="utf-8")


def pgrep(pattern: str) -> list[int]:
    proc = sh(["pgrep", "-f", pattern])
    if proc.returncode not in (0, 1):
        raise RuntimeError(proc.stderr.strip() or f"pgrep failed for {pattern}")
    return [int(line) for line in proc.stdout.splitlines() if line.strip()]


def curl_health() -> tuple[bool, dict | str]:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2, context=_TLS_CTX) as response:
            payload = response.read().decode("utf-8")
            return True, json.loads(payload)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, str(exc)


def wait_for_shutdown(deadline: float, before: dict[str, list[int]]) -> tuple[list[dict[str, object]], bool]:
    timeline: list[dict[str, object]] = []
    old_pids = {pid for pids in before.values() for pid in pids}
    while time.time() < deadline:
        snapshot = {name: pgrep(pattern) for name, pattern in PROCESS_PATTERNS.items()}
        timeline.append({"ts": time.time(), "processes": snapshot})
        current_pids = {pid for pids in snapshot.values() for pid in pids}
        if old_pids and old_pids.isdisjoint(current_pids):
            return timeline, True
        time.sleep(0.5)
    return timeline, False


def wait_for_start(
    deadline: float,
    before: dict[str, list[int]],
) -> tuple[list[dict[str, object]], tuple[bool, dict | str], bool]:
    timeline: list[dict[str, object]] = []
    last_health: tuple[bool, dict | str] = (False, "not_checked")
    old_pids = {pid for pids in before.values() for pid in pids}
    while time.time() < deadline:
        snapshot = {name: pgrep(pattern) for name, pattern in PROCESS_PATTERNS.items()}
        health = curl_health()
        timeline.append({"ts": time.time(), "processes": snapshot, "health": health[1]})
        current_pids = {pid for pids in snapshot.values() for pid in pids}
        has_new_pid = bool(current_pids - old_pids) or not old_pids
        if any(snapshot.values()) and has_new_pid and health[0]:
            return timeline, health, True
        last_health = health
        time.sleep(1.0)
    return timeline, last_health, False


def collect_tail(path: Path, lines: int = 80) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()[-lines:]


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    report_path = LOG_DIR / f"restart-report-{stamp}.json"
    launcher_log = ROOT / ".temp" / "restart-debug" / "launcher.log"

    before = {name: pgrep(pattern) for name, pattern in PROCESS_PATTERNS.items()}
    report: dict[str, object] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "before": before,
        "health_before": curl_health()[1],
    }

    restart = subprocess.run(
        [str(RESTART_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "SYNC_RESTART": "1",
            "AIZE_ROOT": str(ROOT),
            "AIZE_RUNTIME_ROOT": str(RUNTIME_ROOT),
            "AIZE_HTTP_HOST": HTTP_HOST,
            "AIZE_HTTP_PORT": HTTP_PORT,
            "AIZE_NODE_ID": NODE_ID,
        },
    )
    report["restart_stdout"] = restart.stdout.strip()
    report["restart_stderr"] = restart.stderr.strip()
    report["restart_rc"] = restart.returncode

    shutdown_timeline, old_processes_gone = wait_for_shutdown(time.time() + 12, before)
    startup_timeline, final_health, new_process_detected = wait_for_start(time.time() + 20, before)

    report["shutdown_timeline"] = shutdown_timeline
    report["old_processes_gone"] = old_processes_gone
    report["startup_timeline"] = startup_timeline
    report["new_process_detected"] = new_process_detected
    report["health_after"] = final_health[1]
    report["after"] = {name: pgrep(pattern) for name, pattern in PROCESS_PATTERNS.items()}
    report["launcher_log_tail"] = collect_tail(launcher_log)

    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(report_path)

    if restart.returncode != 0:
        return restart.returncode
    if not (final_health[0] and new_process_detected):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
