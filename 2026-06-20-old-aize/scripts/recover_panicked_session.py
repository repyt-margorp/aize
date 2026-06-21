#!/usr/bin/env python3
"""Idempotently clear panic / quarantine state for a single session.

Touches only:
  - .aize-state/sessions/<user>/<sid>/services/*.audit.json    (audit_state: panic -> all_clear)
  - .aize-state/sessions/<user>/<sid>/goal_manager/state.json  (state: failed -> idle)
  - .aize-state/sessions/<user>/<sid>/session.json             (auto_completed_reason / restart_resume_claim_*)

The previous values are saved to <session_dir>/.panic_recovery_backup.json so the
operation can be undone with --rollback. Re-running with --apply on an already-
recovered session is a no-op.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

BACKUP_FILENAME = ".panic_recovery_backup.json"


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_root_default() -> Path:
    return Path(__file__).resolve().parent.parent


def session_dir(state_root: Path, username: str, session_id: str) -> Path:
    return state_root / "sessions" / username / session_id


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".recover.tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    tmp.replace(path)


def collect_panic_state(sdir: Path) -> dict[str, Any]:
    """Snapshot the fields we plan to mutate so rollback can restore them."""
    snapshot: dict[str, Any] = {
        "captured_at": utc_now_iso(),
        "session_json": None,
        "goal_manager_state": None,
        "audits": {},
    }

    session_meta = read_json(sdir / "session.json")
    if isinstance(session_meta, dict):
        snapshot["session_json"] = {
            "auto_completed_reason": session_meta.get("auto_completed_reason", ""),
            "restart_resume_claim_run_id": session_meta.get("restart_resume_claim_run_id", ""),
            "restart_resume_claim_service_id": session_meta.get("restart_resume_claim_service_id", ""),
            "restart_resume_claimed_at": session_meta.get("restart_resume_claimed_at", ""),
            "updated_at": session_meta.get("updated_at", ""),
        }

    gm_state = read_json(sdir / "goal_manager" / "state.json")
    if isinstance(gm_state, dict):
        snapshot["goal_manager_state"] = {
            "state": gm_state.get("state"),
            "audit_state": gm_state.get("audit_state"),
            "updated_at": gm_state.get("updated_at"),
        }

    services_dir = sdir / "services"
    if services_dir.is_dir():
        for audit_path in sorted(services_dir.glob("*.audit.json")):
            audit = read_json(audit_path)
            if isinstance(audit, dict):
                snapshot["audits"][audit_path.name] = {
                    "audit_state": audit.get("audit_state"),
                    "updated_at": audit.get("updated_at"),
                }
    return snapshot


def apply_recovery(sdir: Path, *, now: str) -> dict[str, Any]:
    """Mutate the session files to clear panic / quarantine state.

    Returns a diff dict describing changes performed. Idempotent — fields already
    in the recovered shape are left alone.
    """
    diff: dict[str, Any] = {"audits": {}, "goal_manager_state": None, "session_json": None}

    services_dir = sdir / "services"
    if services_dir.is_dir():
        for audit_path in sorted(services_dir.glob("*.audit.json")):
            audit = read_json(audit_path)
            if not isinstance(audit, dict):
                continue
            current = str(audit.get("audit_state") or "").strip().lower()
            if current == "panic":
                audit["audit_state"] = "all_clear"
                audit["updated_at"] = now
                write_json(audit_path, audit)
                diff["audits"][audit_path.name] = {"audit_state": ["panic", "all_clear"]}

    gm_path = sdir / "goal_manager" / "state.json"
    gm_state = read_json(gm_path)
    if isinstance(gm_state, dict):
        gm_diff: dict[str, Any] = {}
        runtime_state = str(gm_state.get("state") or "").strip().lower()
        if runtime_state == "failed":
            gm_state["state"] = "idle"
            gm_diff["state"] = ["failed", "idle"]
        audit_state = str(gm_state.get("audit_state") or "").strip().lower()
        if audit_state == "panic":
            gm_state["audit_state"] = "needs_compact"
            gm_diff["audit_state"] = ["panic", "needs_compact"]
        if gm_diff:
            gm_state["updated_at"] = now
            write_json(gm_path, gm_state)
            diff["goal_manager_state"] = gm_diff

    session_path = sdir / "session.json"
    session_meta = read_json(session_path)
    if isinstance(session_meta, dict):
        sj_diff: dict[str, Any] = {}
        for key in (
            "auto_completed_reason",
            "restart_resume_claim_run_id",
            "restart_resume_claim_service_id",
            "restart_resume_claimed_at",
        ):
            current = session_meta.get(key)
            if isinstance(current, str) and current:
                session_meta[key] = ""
                sj_diff[key] = [current, ""]
        if sj_diff:
            session_meta["updated_at"] = now
            write_json(session_path, session_meta)
            diff["session_json"] = sj_diff

    return diff


def apply_rollback(sdir: Path, snapshot: dict[str, Any], *, now: str) -> dict[str, Any]:
    diff: dict[str, Any] = {"audits": {}, "goal_manager_state": None, "session_json": None}

    audits = snapshot.get("audits") or {}
    services_dir = sdir / "services"
    for filename, prev in audits.items():
        if not isinstance(prev, dict):
            continue
        audit_path = services_dir / filename
        audit = read_json(audit_path)
        if not isinstance(audit, dict):
            continue
        target_audit_state = prev.get("audit_state")
        if target_audit_state is None:
            continue
        if audit.get("audit_state") != target_audit_state:
            previous = audit.get("audit_state")
            audit["audit_state"] = target_audit_state
            audit["updated_at"] = prev.get("updated_at") or now
            write_json(audit_path, audit)
            diff["audits"][filename] = {"audit_state": [previous, target_audit_state]}

    gm_prev = snapshot.get("goal_manager_state")
    if isinstance(gm_prev, dict):
        gm_path = sdir / "goal_manager" / "state.json"
        gm_state = read_json(gm_path)
        if isinstance(gm_state, dict):
            gm_diff: dict[str, Any] = {}
            for key in ("state", "audit_state"):
                target = gm_prev.get(key)
                if target is None:
                    continue
                if gm_state.get(key) != target:
                    gm_diff[key] = [gm_state.get(key), target]
                    gm_state[key] = target
            if gm_diff:
                gm_state["updated_at"] = gm_prev.get("updated_at") or now
                write_json(gm_path, gm_state)
                diff["goal_manager_state"] = gm_diff

    sj_prev = snapshot.get("session_json")
    if isinstance(sj_prev, dict):
        session_path = sdir / "session.json"
        session_meta = read_json(session_path)
        if isinstance(session_meta, dict):
            sj_diff: dict[str, Any] = {}
            for key in (
                "auto_completed_reason",
                "restart_resume_claim_run_id",
                "restart_resume_claim_service_id",
                "restart_resume_claimed_at",
            ):
                target = sj_prev.get(key)
                if target is None:
                    continue
                if session_meta.get(key) != target:
                    sj_diff[key] = [session_meta.get(key), target]
                    session_meta[key] = target
            if sj_diff:
                session_meta["updated_at"] = sj_prev.get("updated_at") or now
                write_json(session_path, session_meta)
                diff["session_json"] = sj_diff

    return diff


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--username", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument(
        "--state-root",
        default=str(repo_root_default() / ".aize-state"),
        help="Path to .aize-state (defaults to <repo>/.aize-state)",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="Restore the previously-captured panic state from the backup file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print intended actions but do not write any files.",
    )
    args = parser.parse_args(argv)

    state_root = Path(args.state_root).expanduser().resolve()
    sdir = session_dir(state_root, args.username, args.session_id)
    if not sdir.is_dir():
        print(json.dumps({"error": "session_dir_missing", "path": str(sdir)}, ensure_ascii=False))
        return 2

    backup_path = sdir / BACKUP_FILENAME
    now = utc_now_iso()

    if args.rollback:
        if not backup_path.exists():
            print(json.dumps({"error": "no_backup", "path": str(backup_path)}, ensure_ascii=False))
            return 3
        snapshot = read_json(backup_path)
        if not isinstance(snapshot, dict):
            print(json.dumps({"error": "invalid_backup", "path": str(backup_path)}, ensure_ascii=False))
            return 4
        if args.dry_run:
            print(json.dumps({"mode": "rollback_dry_run", "backup": snapshot}, ensure_ascii=False, indent=2))
            return 0
        diff = apply_rollback(sdir, snapshot, now=now)
        backup_path.unlink(missing_ok=True)
        print(json.dumps({"mode": "rollback", "diff": diff}, ensure_ascii=False, indent=2))
        return 0

    snapshot = collect_panic_state(sdir)
    if args.dry_run:
        preview_diff = apply_recovery_preview(sdir)
        print(
            json.dumps(
                {"mode": "apply_dry_run", "would_change": preview_diff, "snapshot": snapshot},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not backup_path.exists():
        write_json(backup_path, snapshot)
    diff = apply_recovery(sdir, now=now)
    summary = {
        "mode": "apply",
        "session_dir": str(sdir),
        "backup_path": str(backup_path),
        "diff": diff,
        "no_op": not (diff["audits"] or diff["goal_manager_state"] or diff["session_json"]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def apply_recovery_preview(sdir: Path) -> dict[str, Any]:
    """Compute the diff a real apply would produce, without writing."""
    diff: dict[str, Any] = {"audits": {}, "goal_manager_state": {}, "session_json": {}}
    services_dir = sdir / "services"
    if services_dir.is_dir():
        for audit_path in sorted(services_dir.glob("*.audit.json")):
            audit = read_json(audit_path)
            if isinstance(audit, dict) and str(audit.get("audit_state") or "").strip().lower() == "panic":
                diff["audits"][audit_path.name] = {"audit_state": ["panic", "all_clear"]}

    gm_state = read_json(sdir / "goal_manager" / "state.json")
    if isinstance(gm_state, dict):
        if str(gm_state.get("state") or "").strip().lower() == "failed":
            diff["goal_manager_state"]["state"] = ["failed", "idle"]
        if str(gm_state.get("audit_state") or "").strip().lower() == "panic":
            diff["goal_manager_state"]["audit_state"] = ["panic", "needs_compact"]

    session_meta = read_json(sdir / "session.json")
    if isinstance(session_meta, dict):
        for key in (
            "auto_completed_reason",
            "restart_resume_claim_run_id",
            "restart_resume_claim_service_id",
            "restart_resume_claimed_at",
        ):
            current = session_meta.get(key)
            if isinstance(current, str) and current:
                diff["session_json"][key] = [current, ""]
    return diff


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
