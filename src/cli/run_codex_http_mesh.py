from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
import re
from collections import defaultdict

from services.svcmgr.loader import build_service_plan, list_service_descriptors

# Accept --runtime-root as a CLI arg so the runtime root appears in the process
# command line, enabling per-instance pgrep matching in restart scripts.
_cli_runtime_root: str | None = None
for _i, _arg in enumerate(sys.argv[1:], 1):
    if _arg == "--runtime-root" and _i < len(sys.argv):
        _cli_runtime_root = sys.argv[_i + 1]
        break
    if _arg.startswith("--runtime-root="):
        _cli_runtime_root = _arg.split("=", 1)[1]
        break

ROOT = Path(os.environ.get("AIZE_ROOT", Path(__file__).resolve().parents[2]))
RUNTIME_ROOT = Path(
    _cli_runtime_root
    or os.environ.get("AIZE_RUNTIME_ROOT", str(ROOT / ".agent-mesh-runtime"))
)
PORTS = RUNTIME_ROOT / "ports"
LOGS = RUNTIME_ROOT / "logs"
OBJECTS = RUNTIME_ROOT / "objects"
STATE = RUNTIME_ROOT / "state"
MANIFEST = RUNTIME_ROOT / "manifest.json"
NODE_ID = os.environ.get("AIZE_NODE_ID") or f"node-{re.sub(r'[^a-z0-9]+', '-', ROOT.name.lower()).strip('-') or 'local'}"
PRIMARY_RUNTIME_ROOT = (ROOT / ".agent-mesh-runtime").resolve()
ALLOW_PRIMARY_HTTP_OVERRIDE = os.environ.get("AIZE_ALLOW_PRIMARY_RUNTIME_HTTP_OVERRIDE", "").strip().lower() in {"1", "true", "yes", "on"}
if RUNTIME_ROOT.resolve() == PRIMARY_RUNTIME_ROOT and not ALLOW_PRIMARY_HTTP_OVERRIDE:
    HTTP_HOST = "0.0.0.0"
    HTTP_PORT = 4123
    HTTP_TLS = True
else:
    HTTP_HOST = os.environ.get("AIZE_HTTP_HOST", "0.0.0.0")
    HTTP_PORT = int(os.environ.get("AIZE_HTTP_PORT", "4123"))
    HTTP_TLS = os.environ.get("AIZE_TLS", "true").strip().lower() not in {"false", "0", "no", "off"}
CORE_SERVICE_IDS = {"service-http-001", "service-svcmgr-001"} | {
    f"service-codex-{index:03d}" for index in range(1, int(os.environ.get("AIZE_CODEX_POOL_SIZE", "5")) + 1)
} | {
    f"service-claude-{index:03d}" for index in range(1, int(os.environ.get("AIZE_CLAUDE_POOL_SIZE", "5")) + 1)
}


def build_core_manifest() -> dict:
    descriptors = list_service_descriptors(exclude_kinds={"svcmgr"})
    services = build_service_plan(descriptors)
    routes: list[dict] = []
    service_ids = [str(service["service_id"]) for service in services]
    for sender_id in service_ids:
        for recipient_id in service_ids:
            if sender_id == recipient_id:
                continue
            routes.append(
                {
                    "sender_id": sender_id,
                    "recipient_id": recipient_id,
                    "enabled": True,
                }
            )
    return {
        "node_id": NODE_ID,
        "run_id": f"run-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}",
        "peer": {
            "peer_id": f"peer-{uuid.uuid4().hex}",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "settings": {
            "inline_payload_max_bytes": 4096,
        },
        "services": services,
        "routes": routes,
    }


def apply_restart_resume_to_manifest_services(manifest: dict, restart_resume: dict[str, dict]) -> dict:
    services = []
    for service in manifest.get("services", []):
        if not isinstance(service, dict):
            continue
        service_id = str(service.get("service_id", "")).strip()
        config = dict(service.get("config", {}))
        if service_id:
            config["restart_resume"] = restart_resume.get(service_id)
        merged = dict(service)
        if config:
            merged["config"] = config
        services.append(merged)
    merged_manifest = dict(manifest)
    merged_manifest["services"] = services
    return merged_manifest


def load_restart_resume_map() -> dict[str, dict]:
    services_path = STATE / "services.json"
    processes_path = STATE / "processes.json"
    if not services_path.exists():
        return {}
    try:
        services_state = json.loads(services_path.read_text(encoding="utf-8"))
        processes_state = json.loads(processes_path.read_text(encoding="utf-8")) if processes_path.exists() else {"processes": {}}
    except json.JSONDecodeError:
        return {}
    services = services_state.get("services", {})
    processes = processes_state.get("processes", {})
    if not isinstance(services, dict) or not isinstance(processes, dict):
        return {}

    restart_resume: dict[str, dict] = {}
    for service_id, record in services.items():
        if not isinstance(record, dict):
            continue
        process_id = record.get("current_process_id")
        process = processes.get(str(process_id), {}) if process_id else {}
        restart_resume[str(service_id)] = {
            "mode": "system_restart",
            "previous_process_id": process_id,
            "previous_status": process.get("status") or record.get("status"),
            "previous_reason": process.get("reason"),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    return restart_resume


def capture_restorable_runtime() -> tuple[list[dict], list[dict], dict[str, dict]]:
    restart_resume = load_restart_resume_map()
    services_path = STATE / "services.json"
    if not services_path.exists():
        return [], [], restart_resume
    try:
        state = json.loads(services_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [], [], restart_resume
    services = state.get("services", {})
    if not isinstance(services, dict):
        return [], [], restart_resume

    restored_ids = {
        service_id
        for service_id, record in services.items()
        if service_id not in CORE_SERVICE_IDS and isinstance(record, dict) and str(record.get("status", "")) != "stopped"
    }
    extra_services: list[dict] = []
    for service_id in sorted(restored_ids):
        record = services.get(service_id) or {}
        extra_services.append(
            {
                "service_id": service_id,
                "kind": str(record.get("kind", "")),
                "display_name": str(record.get("display_name", service_id)),
                "persona": str(record.get("persona", "")),
                "max_turns": int(record.get("max_turns", 100)),
                "response_schema_id": record.get("response_schema_id"),
                "config": {
                    **dict(record.get("config", {})),
                    "restart_resume": restart_resume.get(service_id),
                },
                "owner_principal": record.get("owner_principal"),
                "owner_roles": list(record.get("owner_roles", [])),
                "owner_capabilities": list(record.get("owner_capabilities", [])),
            }
        )

    extra_routes: list[dict] = []
    route_keys: set[tuple[str, str]] = set()
    for sender_id, record in services.items():
        if not isinstance(record, dict):
            continue
        for recipient_id in record.get("allowed_peers", []):
            if not isinstance(recipient_id, str):
                continue
            if sender_id not in restored_ids and recipient_id not in restored_ids:
                continue
            key = (str(sender_id), recipient_id)
            if key in route_keys:
                continue
            route_keys.add(key)
            extra_routes.append(
                {
                    "sender_id": str(sender_id),
                    "recipient_id": recipient_id,
                    "enabled": True,
                }
            )
    return extra_services, extra_routes, restart_resume


def write_manifest(
    *,
    extra_services: list[dict] | None = None,
    extra_routes: list[dict] | None = None,
    restart_resume: dict[str, dict] | None = None,
) -> dict:
    manifest = build_core_manifest()
    if restart_resume:
        manifest = apply_restart_resume_to_manifest_services(manifest, restart_resume)
    manifest_services = list(manifest.get("services", []))
    existing_ids = {str(service.get("service_id")) for service in manifest_services}
    for extra in extra_services or []:
        if str(extra.get("service_id")) in existing_ids:
            continue
        manifest_services.append(extra)
    manifest["services"] = manifest_services
    manifest["routes"] = list(manifest.get("routes", [])) + list(extra_routes or [])
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def bootstrap_runtime() -> dict:
    extra_services, extra_routes, restart_resume = capture_restorable_runtime()
    if RUNTIME_ROOT.exists():
        # Preserve TLS certificates across restarts so manually-generated certs survive.
        tls_dir = RUNTIME_ROOT / "tls"
        saved_tls: dict[str, bytes] = {}
        if tls_dir.exists():
            for f in tls_dir.iterdir():
                if f.is_file():
                    saved_tls[f.name] = f.read_bytes()
        # Preserve ws_peer_clients.json (outbound peer client config).
        ws_peer_clients_path = RUNTIME_ROOT / "ws_peer_clients.json"
        saved_ws_peer_clients: bytes | None = (
            ws_peer_clients_path.read_bytes() if ws_peer_clients_path.exists() else None
        )
        shutil.rmtree(RUNTIME_ROOT)
    else:
        saved_tls = {}
        saved_ws_peer_clients = None
    PORTS.mkdir(parents=True)
    LOGS.mkdir(parents=True)
    OBJECTS.mkdir(parents=True)
    STATE.mkdir(parents=True)
    if saved_tls:
        tls_restore = RUNTIME_ROOT / "tls"
        tls_restore.mkdir(parents=True, exist_ok=True)
        for name, data in saved_tls.items():
            dest = tls_restore / name
            dest.write_bytes(data)
            dest.chmod(0o600 if name.endswith(".key") else 0o644)
    if saved_ws_peer_clients is not None:
        (RUNTIME_ROOT / "ws_peer_clients.json").write_bytes(saved_ws_peer_clients)
    for service in extra_services:
        if not isinstance(service, dict):
            continue
        config = dict(service.get("config", {}))
        service["config"] = {
            **config,
            "restart_resume": restart_resume.get(str(service.get("service_id"))),
        }
    manifest = write_manifest(
        extra_services=extra_services,
        extra_routes=extra_routes,
        restart_resume=restart_resume,
    )
    return manifest


def spawn_logged(name: str, *argv: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    # Propagate HTTP config as env vars so service.json config_env picks them up
    env["AIZE_HTTP_HOST"] = HTTP_HOST
    env["AIZE_HTTP_PORT"] = str(HTTP_PORT)
    env["AIZE_TLS"] = "true" if HTTP_TLS else "false"
    LOGS.mkdir(parents=True, exist_ok=True)
    stdout_handle = (LOGS / f"{name}.stdout.log").open("a", encoding="utf-8")
    stderr_handle = (LOGS / f"{name}.stderr.log").open("a", encoding="utf-8")
    proc = subprocess.Popen(
        list(argv),
        cwd=str(ROOT),
        env=env,
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
        encoding="utf-8",
    )
    proc._aize_stdout_handle = stdout_handle  # type: ignore[attr-defined]
    proc._aize_stderr_handle = stderr_handle  # type: ignore[attr-defined]
    return proc


def spawn_router_process() -> subprocess.Popen:
    return spawn_logged(
        "kernel.router",
        sys.executable,
        "-m",
        "kernel.router",
        "--manifest",
        str(MANIFEST),
        "--runtime-root",
        str(RUNTIME_ROOT),
    )


def spawn_service_process(service_id: str) -> subprocess.Popen:
    return spawn_logged(
        service_id,
        sys.executable,
        "-m",
        "runtime.cli_service_adapter",
        "--manifest",
        str(MANIFEST),
        "--runtime-root",
        str(RUNTIME_ROOT),
        "--service-id",
        service_id,
    )


def main() -> int:
    manifest = bootstrap_runtime()
    procs: dict[str, subprocess.Popen] = {"kernel.router": spawn_router_process()}
    for service in manifest.get("services", []):
        service_id = str(service.get("service_id", "")).strip()
        if not service_id:
            continue
        procs[service_id] = spawn_service_process(service_id)
    restart_window_seconds = 10.0
    restart_limit = 3
    restart_history: dict[str, list[float]] = defaultdict(list)
    stop_requested = False

    def request_stop(signum: int, _frame: object | None) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    print(
        json.dumps(
            {
                "node_id": manifest["node_id"],
                "run_id": manifest["run_id"],
                "runtime_root": str(RUNTIME_ROOT),
                "http": {
                    "health": f"http://{HTTP_HOST}:{HTTP_PORT}/health",
                    "messages": f"http://{HTTP_HOST}:{HTTP_PORT}/messages",
                    "post_message": f"curl -s -X POST http://{HTTP_HOST}:{HTTP_PORT}/message -H 'Content-Type: application/json' -d '{{\"text\":\"Hello CodexFox\"}}'",
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    try:
        while not stop_requested:
            time.sleep(1.0)
            for name, proc in list(procs.items()):
                if proc.poll() is not None:
                    now = time.monotonic()
                    recent = [
                        ts
                        for ts in restart_history[name]
                        if now - ts <= restart_window_seconds
                    ]
                    recent.append(now)
                    restart_history[name] = recent
                    if len(recent) > restart_limit:
                        continue
                    if name == "kernel.router":
                        procs[name] = spawn_router_process()
                    else:
                        procs[name] = spawn_service_process(name)
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in procs.values():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            stdout_handle = getattr(proc, "_aize_stdout_handle", None)
            stderr_handle = getattr(proc, "_aize_stderr_handle", None)
            if stdout_handle is not None:
                stdout_handle.close()
            if stderr_handle is not None:
                stderr_handle.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
