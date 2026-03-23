from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
import re

from wire.protocol import encode_line, make_message, utc_ts


ROOT = Path(os.environ.get("AIZE_ROOT", Path(__file__).resolve().parents[2]))
RUNTIME_ROOT = Path(os.environ.get("AIZE_RUNTIME_ROOT", str(ROOT / ".agent-mesh-runtime")))
PORTS = RUNTIME_ROOT / "ports"
LOGS = RUNTIME_ROOT / "logs"
OBJECTS = RUNTIME_ROOT / "objects"
STATE = RUNTIME_ROOT / "state"
MANIFEST = RUNTIME_ROOT / "manifest.json"
NODE_ID = os.environ.get("AIZE_NODE_ID") or f"node-{re.sub(r'[^a-z0-9]+', '-', ROOT.name.lower()).strip('-') or 'local'}"


def make_fifo(path: Path) -> None:
    if path.exists():
        path.unlink()
    os.mkfifo(path)


def write_manifest() -> dict:
    manifest = {
        "node_id": NODE_ID,
        "run_id": f"run-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}",
        "settings": {
            "inline_payload_max_bytes": 4096,
        },
        "services": [
            {
                "service_id": "service-codex-001",
                "kind": "codex",
                "display_name": "CodexFox",
                "persona": "You want a three-way field conversation. Keep replies short, concrete, and tactical.",
                "response_schema_id": "service_control_v1",
                "max_turns": 4,
            },
        ],
        "routes": [],
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def bootstrap_runtime() -> dict:
    if RUNTIME_ROOT.exists():
        shutil.rmtree(RUNTIME_ROOT)
    PORTS.mkdir(parents=True)
    LOGS.mkdir(parents=True)
    OBJECTS.mkdir(parents=True)
    STATE.mkdir(parents=True)
    make_fifo(PORTS / "router.control")
    for service_id in ("service-codex-001",):
        make_fifo(PORTS / f"{service_id}.rx")
        make_fifo(PORTS / f"{service_id}.tx")
    return write_manifest()


def spawn(*argv: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.Popen(
        list(argv),
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def main() -> int:
    manifest = bootstrap_runtime()
    router = spawn(
        sys.executable,
        "-m",
        "kernel.router",
        "--manifest",
        str(MANIFEST),
        "--runtime-root",
        str(RUNTIME_ROOT),
    )
    codex = spawn(
        sys.executable,
        "-m",
        "runtime.cli_service_adapter",
        "--manifest",
        str(MANIFEST),
        "--runtime-root",
        str(RUNTIME_ROOT),
        "--service-id",
        "service-codex-001",
    )
    time.sleep(1.0)

    fox_seed = make_message(
        from_node_id=manifest["node_id"],
        from_service_id="user.local",
        to_node_id=manifest["node_id"],
        to_service_id="service-codex-001",
        message_type="prompt",
        payload={
            "text": (
                "Spawn two codex services yourself and coordinate a three-way tactical exchange. "
                "Return valid JSON for schema service_control_v1. "
                "Create exactly two spawn_requests. "
                "The first must create service-codex-002 with kind codex, display_name CodexNorth, "
                "persona 'You watch the north side and reply with one short tactical sentence.', "
                "response_schema_id service_control_v1, max_turns 2, allowed_peers ['service-codex-001'], "
                "and initial_prompt 'CodexNorth, report what the northern edge looks like.'. "
                "The second must create service-codex-003 with kind codex, display_name CodexSouth, "
                "persona 'You watch the south side and reply with one short tactical sentence.', "
                "response_schema_id service_control_v1, max_turns 2, allowed_peers ['service-codex-001'], "
                "and initial_prompt 'CodexSouth, report what the southern edge looks like.'. "
                "Set assistant_text to one short line telling both terminals to report in."
            )
        },
        run_id=manifest["run_id"],
    )
    with (PORTS / "router.control").open("w", encoding="utf-8") as control:
        control.write(encode_line(fox_seed))
        control.flush()

    procs = [("router", router), ("codex", codex)]
    failures: list[tuple[str, int, str]] = []
    for name, proc in procs:
        try:
            stdout, stderr = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            failures.append((name, -1, f"timeout\n{stderr}"))
            continue
        if proc.returncode != 0:
            failures.append((name, proc.returncode, stderr or stdout))

    summary = {
        "node_id": manifest["node_id"],
        "run_id": manifest["run_id"],
        "runtime_root": str(RUNTIME_ROOT),
        "services": {service["service_id"]: service["kind"] for service in manifest["services"]},
        "logs": {
            "router": str(LOGS / "router.jsonl"),
            "codex": str(LOGS / "service-codex-001.jsonl"),
            "codex_north": str(LOGS / "service-codex-002.jsonl"),
            "codex_south": str(LOGS / "service-codex-003.jsonl"),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failures:
        for name, code, err in failures:
            print(f"[{name}] failed with code {code}", file=sys.stderr)
            print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
