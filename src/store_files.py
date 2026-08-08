from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import struct
import time
import zlib
from pathlib import Path
from typing import Any, Iterable

from model import new_id
from store_defs import STATE_VERSION, StoreError, safe_id_part


STORAGE_VERSION = 2
FRAME_MAGIC = b"AIZELOG2"
FRAME_HEADER = struct.Struct(">8sQI")
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_SEGMENT_BYTES = 64 * 1024 * 1024

ITEM_COLLECTION_KEYS = {
    "accounts": "username",
    "units": "unit_id",
    "sessions": "session_id",
    "goals": "goal_id",
    "agent_profiles": "role",
    "agent_threads": "thread_id",
    "dispatch_runs": "run_id",
}
VALUE_COLLECTIONS = {
    "session_edges": [],
    "dispatch_requests": [],
    "runtime_settings": {},
    "endpoint_cursors": {},
}


class SplitStateStorage:
    """Durable item metadata plus framed append-only logs per Session."""

    def __init__(self, root: Path, *, data_name: str = "store") -> None:
        self.root = root
        self.manifest_path = root / "manifest.json"
        self.legacy_state_path = root / "state.json"
        self.data_root = root / data_name
        self.metadata_root = self.data_root / "metadata"
        self.runtime_root = self.data_root / "runtime"
        self.session_root = self.data_root / "sessions"
        self.artifacts_root = self.data_root / "artifacts"

    def is_initialized(self) -> bool:
        return self.manifest_path.exists()

    def ensure_current_format(self) -> bool:
        if not self.manifest_path.exists():
            return False
        manifest = self._read_json_object(self.manifest_path)
        version = manifest.get("storage_version")
        if version == STORAGE_VERSION:
            return False
        if version != 1:
            raise StoreError(f"unsupported storage version: {version}")
        state = self._load_generation_state(manifest)
        self._hydrate_legacy_transcripts(state)
        self._embed_legacy_messages(state)
        self._install_migrated_state(state, backup_manifest=manifest)
        return True

    def migrate_legacy_state_json(self) -> Path:
        state = self._read_json_object(self.legacy_state_path)
        if state.get("version") != STATE_VERSION:
            raise StoreError(f"unsupported legacy state version: {state.get('version')}")
        self._embed_legacy_messages(state)
        self._install_migrated_state(state)
        backup = self.legacy_state_path.with_name("state.v1.json")
        if backup.exists():
            backup = self.legacy_state_path.with_name(f"state.v1-{time.time_ns()}.json")
        self.legacy_state_path.replace(backup)
        backup.chmod(0o600)
        return backup

    def initialize(self, state: dict[str, Any]) -> None:
        if self.manifest_path.exists():
            raise StoreError(f"state already initialized: {self.manifest_path}")
        self._prepare_data_directories()
        self.save(state)
        self._write_manifest(state)

    def load(self) -> dict[str, Any]:
        manifest = self._read_json_object(self.manifest_path)
        if manifest.get("storage_version") != STORAGE_VERSION:
            raise StoreError(f"unsupported storage version: {manifest.get('storage_version')}")
        if manifest.get("state_version") != STATE_VERSION:
            raise StoreError(f"unsupported state version: {manifest.get('state_version')}")
        state: dict[str, Any] = {
            "version": manifest["state_version"],
            "created_at": manifest["created_at"],
        }
        for name, identity_key in ITEM_COLLECTION_KEYS.items():
            state[name] = self._load_item_collection(name, identity_key)
        for name, default in VALUE_COLLECTIONS.items():
            path = self._value_collection_path(name)
            state[name] = self._read_json(path) if path.exists() else copy.deepcopy(default)
        # Session messages are embedded in SessionLog. These are operation-local caches.
        state["messages"] = []
        state["session_logs"] = {}
        return state

    def save(self, state: dict[str, Any]) -> None:
        if state.get("version") != STATE_VERSION:
            raise StoreError(f"unsupported state version: {state.get('version')}")
        self._prepare_data_directories()

        for session_id, entries in state.get("session_logs", {}).items():
            if isinstance(entries, list):
                self.append_session_log_entries(str(session_id), entries)

        for name, identity_key in ITEM_COLLECTION_KEYS.items():
            value = state.get(name, {})
            if not isinstance(value, dict):
                raise StoreError(f"state collection {name} must be an object")
            self._save_item_collection(name, identity_key, value)
        for name, default in VALUE_COLLECTIONS.items():
            self._write_json_atomic_if_changed(
                self._value_collection_path(name),
                state.get(name, copy.deepcopy(default)),
            )
        if not self.manifest_path.exists():
            self._write_manifest(state)

    def read_session_log(
        self,
        session_id: str,
        *,
        from_seq: int | None = None,
        to_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for segment in self._segment_paths_for_range(session_id, from_seq=from_seq, to_seq=to_seq):
            segment_entries, _, _ = self._scan_segment(segment, recover=False)
            for entry in segment_entries:
                seq = int(entry.get("seq") or 0)
                if from_seq is not None and seq < from_seq:
                    continue
                if to_seq is not None and seq > to_seq:
                    continue
                entries.append(entry)
        if entries:
            self._validate_sequence(
                entries,
                session_id=session_id,
                expected_start=int(entries[0].get("seq") or 0),
            )
        return entries

    def latest_session_log_seq(self, session_id: str) -> int:
        index_path = self._session_index_path(session_id)
        if not index_path.exists():
            return 0
        return int(self._read_json_object(index_path).get("latest_seq") or 0)

    def append_session_log_entries(self, session_id: str, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        index = self._recover_session_log(session_id)
        persisted_seq = int(index.get("latest_seq") or 0)
        pending = [dict(entry) for entry in entries if int(entry.get("seq") or 0) > persisted_seq]
        if not pending:
            return
        expected = persisted_seq + 1
        for entry in pending:
            seq = int(entry.get("seq") or 0)
            if seq != expected:
                raise StoreError(
                    f"SessionLog sequence mismatch for {session_id}: expected {expected}, got {seq}"
                )
            if str(entry.get("session_id") or "") != session_id:
                raise StoreError(f"SessionLog entry belongs to another Session: {session_id}")
            expected += 1

        frames = [self._encode_frame(entry) for entry in pending]
        segment_path = self._active_segment_path(session_id, index, first_seq=int(pending[0]["seq"]))
        existing_size = segment_path.stat().st_size if segment_path.exists() else 0
        frame_bytes = sum(len(frame) for frame in frames)
        if existing_size and existing_size + frame_bytes > MAX_SEGMENT_BYTES:
            segment_path = self._new_segment_path(session_id, int(pending[0]["seq"]))
            existing_size = 0
        segment_path.parent.mkdir(parents=True, exist_ok=True)
        self._secure_directory_tree(segment_path.parent, stop=self.root)
        descriptor = os.open(segment_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            payload = b"".join(frames)
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fdatasync(descriptor)
        finally:
            os.close(descriptor)

        relative_segment = str(segment_path.relative_to(self._session_storage_path(session_id)))
        segments = index.setdefault("segments", [])
        segment_index = next(
            (
                item
                for item in segments
                if isinstance(item, dict) and str(item.get("path") or "") == relative_segment
            ),
            None,
        )
        if segment_index is None:
            segment_index = {
                "path": relative_segment,
                "start_seq": int(pending[0]["seq"]),
                "end_seq": persisted_seq,
                "records": 0,
                "bytes": existing_size,
            }
            segments.append(segment_index)
        segment_index["end_seq"] = int(pending[-1]["seq"])
        segment_index["records"] = int(segment_index.get("records") or 0) + len(pending)
        segment_index["bytes"] = existing_size + frame_bytes
        index["latest_seq"] = int(pending[-1]["seq"])
        index["record_count"] = int(index.get("record_count") or 0) + len(pending)
        index["message_count"] = int(index.get("message_count") or 0) + sum(
            1 for entry in pending if entry.get("kind") == "Message"
        )
        self._write_json_atomic(self._session_index_path(session_id), index)

    def replace_session_log(self, session_id: str, entries: list[dict[str, Any]]) -> None:
        target = self._session_storage_path(session_id)
        staging = target.with_name(f"{target.name}.migrating-{new_id('log')}")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=False)
        staging_storage = _SessionDirectoryWriter(staging)
        staging_storage.write(entries)
        if target.exists():
            backup = target.with_name(f"{target.name}.replaced-{time.time_ns()}")
            target.replace(backup)
            staging.replace(target)
            shutil.rmtree(backup)
        else:
            staging.replace(target)
        self._fsync_directory(target.parent)

    def recover_all_session_logs(self) -> dict[str, int]:
        recovered: dict[str, int] = {}
        if not self.session_root.exists():
            return recovered
        for index_path in self.session_root.glob("*/log.index.json"):
            index = self._read_json_object(index_path)
            session_id = str(index.get("session_id") or "")
            if session_id:
                rebuilt = self._recover_session_log(session_id, full=True)
                recovered[session_id] = int(rebuilt.get("latest_seq") or 0)
        return recovered

    def session_log_ids(self) -> list[str]:
        session_ids: list[str] = []
        if not self.session_root.exists():
            return session_ids
        for path in self.session_root.glob("*/log.index.json"):
            try:
                index = self._read_json_object(path)
            except StoreError:
                continue
            session_id = str(index.get("session_id") or "")
            if session_id:
                session_ids.append(session_id)
        return sorted(set(session_ids))

    def session_log_stats(self) -> dict[str, int]:
        record_count = 0
        message_count = 0
        for session_id in self.session_log_ids():
            index = self._read_json_object(self._session_index_path(session_id))
            record_count += int(index.get("record_count") or 0)
            message_count += int(index.get("message_count") or 0)
        return {"record_count": record_count, "message_count": message_count}

    def hydrate_agent_threads(self, threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hydrated = copy.deepcopy(threads)
        for thread in hydrated:
            for turn in thread.get("turns", []):
                self._hydrate_field(turn, "prompt", "prompt_path")
                self._hydrate_field(turn, "result", "result_path")
        return hydrated

    def hydrate_dispatch_runs(self, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hydrated = copy.deepcopy(runs)
        for run in hydrated:
            for step in run.get("steps", []):
                self._hydrate_field(step, "output", "output_path")
        return hydrated

    def _save_item_collection(
        self,
        name: str,
        identity_key: str,
        collection: dict[str, Any],
    ) -> None:
        root = self._item_collection_root(name)
        root.mkdir(parents=True, exist_ok=True)
        self._secure_directory_tree(root, stop=self.root)
        expected_paths: set[Path] = set()
        for key, item in collection.items():
            if not isinstance(item, dict):
                raise StoreError(f"state item {name}:{key} must be an object")
            persisted = copy.deepcopy(item)
            persisted.setdefault(identity_key, str(key))
            if name == "agent_threads":
                for turn in persisted.get("turns", []):
                    if isinstance(turn, dict):
                        self._externalize_field(turn, "prompt", "prompt_path")
                        self._externalize_field(turn, "result", "result_path")
            elif name == "dispatch_runs":
                for step in persisted.get("steps", []):
                    if isinstance(step, dict):
                        self._externalize_field(step, "output", "output_path")
            path = root / f"{self._key_digest(str(key))}.json"
            expected_paths.add(path)
            self._write_json_atomic_if_changed(path, persisted)
        for path in root.glob("*.json"):
            if path not in expected_paths:
                path.unlink()
        self._fsync_directory(root)

    def _load_item_collection(self, name: str, identity_key: str) -> dict[str, Any]:
        collection: dict[str, Any] = {}
        root = self._item_collection_root(name)
        if not root.exists():
            return collection
        for path in sorted(root.glob("*.json")):
            item = self._read_json_object(path)
            key = str(item.get(identity_key) or "")
            if not key:
                raise StoreError(f"state item has no {identity_key}: {path}")
            collection[key] = item
        return collection

    def _recover_session_log(self, session_id: str, *, full: bool = False) -> dict[str, Any]:
        index_path = self._session_index_path(session_id)
        if not full and index_path.exists():
            index = self._read_json_object(index_path)
            segments = index.get("segments")
            if isinstance(segments, list) and segments:
                last = segments[-1]
                relative_path = str(last.get("path") or "") if isinstance(last, dict) else ""
                segment_path = self._session_storage_path(session_id) / relative_path
                indexed_bytes = int(last.get("bytes") or 0) if isinstance(last, dict) else -1
                if segment_path.exists() and segment_path.stat().st_size == indexed_bytes:
                    return index
        rebuilt = self._build_session_log_index(session_id, recover=True)
        self._write_json_atomic(index_path, rebuilt)
        return rebuilt

    def _build_session_log_index(self, session_id: str, *, recover: bool = False) -> dict[str, Any]:
        segments: list[dict[str, Any]] = []
        expected_seq = 1
        message_count = 0
        record_count = 0
        for path in self._segment_paths(session_id):
            entries, valid_bytes, truncated = self._scan_segment(path, recover=recover)
            if truncated and not recover:
                raise StoreError(f"torn SessionLog frame: {path}")
            if entries:
                if int(entries[0].get("seq") or 0) != expected_seq:
                    raise StoreError(
                        f"SessionLog sequence mismatch for {session_id}: expected {expected_seq}"
                    )
                self._validate_sequence(entries, session_id=session_id, expected_start=expected_seq)
                expected_seq = int(entries[-1]["seq"]) + 1
            record_count += len(entries)
            message_count += sum(1 for entry in entries if entry.get("kind") == "Message")
            segments.append(
                {
                    "path": str(path.relative_to(self._session_storage_path(session_id))),
                    "start_seq": int(entries[0]["seq"]) if entries else expected_seq,
                    "end_seq": int(entries[-1]["seq"]) if entries else expected_seq - 1,
                    "records": len(entries),
                    "bytes": valid_bytes,
                }
            )
        return {
            "version": 1,
            "session_id": session_id,
            "latest_seq": expected_seq - 1,
            "record_count": record_count,
            "message_count": message_count,
            "segments": segments,
        }

    def _scan_segment(self, path: Path, *, recover: bool) -> tuple[list[dict[str, Any]], int, bool]:
        entries: list[dict[str, Any]] = []
        valid_bytes = 0
        truncated = False
        with path.open("rb") as handle:
            while True:
                header = handle.read(FRAME_HEADER.size)
                if not header:
                    break
                if len(header) != FRAME_HEADER.size:
                    truncated = True
                    break
                magic, length, checksum = FRAME_HEADER.unpack(header)
                if magic != FRAME_MAGIC or length > MAX_FRAME_BYTES:
                    truncated = True
                    break
                payload = handle.read(length)
                if len(payload) != length or zlib.crc32(payload) & 0xFFFFFFFF != checksum:
                    truncated = True
                    break
                try:
                    entry = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    truncated = True
                    break
                if not isinstance(entry, dict):
                    truncated = True
                    break
                entries.append(entry)
                valid_bytes += FRAME_HEADER.size + length
        if truncated and recover:
            with path.open("r+b") as handle:
                handle.truncate(valid_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        return entries, valid_bytes, truncated

    def _encode_frame(self, entry: dict[str, Any]) -> bytes:
        payload = json.dumps(
            entry,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_FRAME_BYTES:
            raise StoreError(f"SessionLog record exceeds {MAX_FRAME_BYTES} bytes")
        checksum = zlib.crc32(payload) & 0xFFFFFFFF
        return FRAME_HEADER.pack(FRAME_MAGIC, len(payload), checksum) + payload

    def _validate_sequence(
        self,
        entries: list[dict[str, Any]],
        *,
        session_id: str,
        expected_start: int = 1,
    ) -> None:
        expected = expected_start
        for entry in entries:
            if str(entry.get("session_id") or "") != session_id:
                raise StoreError(f"SessionLog entry belongs to another Session: {session_id}")
            seq = int(entry.get("seq") or 0)
            if seq != expected:
                raise StoreError(
                    f"SessionLog sequence mismatch for {session_id}: expected {expected}, got {seq}"
                )
            expected += 1

    def _active_segment_path(self, session_id: str, index: dict[str, Any], *, first_seq: int) -> Path:
        segments = index.get("segments")
        if isinstance(segments, list) and segments:
            last = segments[-1]
            if isinstance(last, dict) and last.get("path"):
                return self._session_storage_path(session_id) / str(last["path"])
        return self._new_segment_path(session_id, first_seq)

    def _new_segment_path(self, session_id: str, start_seq: int) -> Path:
        return self._session_storage_path(session_id) / "log" / f"{start_seq:020d}.aizelog"

    def _segment_paths(self, session_id: str) -> list[Path]:
        root = self._session_storage_path(session_id) / "log"
        return sorted(root.glob("*.aizelog")) if root.exists() else []

    def _segment_paths_for_range(
        self,
        session_id: str,
        *,
        from_seq: int | None,
        to_seq: int | None,
    ) -> list[Path]:
        index_path = self._session_index_path(session_id)
        if not index_path.exists() or (from_seq is None and to_seq is None):
            return self._segment_paths(session_id)
        index = self._read_json_object(index_path)
        selected: list[Path] = []
        for segment in index.get("segments", []):
            if not isinstance(segment, dict):
                continue
            start = int(segment.get("start_seq") or 0)
            end = int(segment.get("end_seq") or 0)
            if from_seq is not None and end < from_seq:
                continue
            if to_seq is not None and start > to_seq:
                continue
            selected.append(self._session_storage_path(session_id) / str(segment.get("path") or ""))
        return selected

    def _session_storage_path(self, session_id: str) -> Path:
        safe = safe_id_part(session_id, fallback="session")[:80]
        return self.session_root / f"{safe}-{self._key_digest(session_id)[:12]}"

    def _session_index_path(self, session_id: str) -> Path:
        return self._session_storage_path(session_id) / "log.index.json"

    def _item_collection_root(self, name: str) -> Path:
        base = self.runtime_root if name in {"agent_threads", "dispatch_runs"} else self.metadata_root
        return base / name

    def _value_collection_path(self, name: str) -> Path:
        base = self.runtime_root if name in {"dispatch_requests", "endpoint_cursors"} else self.metadata_root
        return base / f"{name}.json"

    def _externalize_field(self, record: dict[str, Any], field: str, path_field: str) -> None:
        value = record.pop(field, None)
        if value is None:
            return
        text = str(value)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        relative_path = self.artifacts_root.relative_to(self.root) / digest[:2] / f"{digest}.txt"
        artifact_path = self.root / relative_path
        if not artifact_path.exists():
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            self._secure_directory_tree(artifact_path.parent, stop=self.root)
            self._write_text_atomic(artifact_path, text)
        record[path_field] = relative_path.as_posix()
        record[f"{field}_chars"] = len(text)

    def _hydrate_field(self, record: dict[str, Any], field: str, path_field: str) -> None:
        if field in record:
            return
        relative_path = record.get(path_field)
        if not isinstance(relative_path, str) or not relative_path:
            return
        path = (self.root / relative_path).resolve()
        root = self.root.resolve()
        if path != root and root not in path.parents:
            raise StoreError(f"artifact path escapes state root: {relative_path}")
        try:
            record[field] = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise StoreError(f"missing state artifact: {relative_path}") from exc

    def _install_migrated_state(
        self,
        state: dict[str, Any],
        *,
        backup_manifest: dict[str, Any] | None = None,
    ) -> None:
        staging_name = f"store.migrating-{new_id('state')}"
        staging = SplitStateStorage(self.root, data_name=staging_name)
        staging._prepare_data_directories()
        for session_id, entries in state.get("session_logs", {}).items():
            if isinstance(entries, list) and entries:
                staging.replace_session_log(str(session_id), entries)
        staging.save(state)
        if self.data_root.exists():
            raise StoreError(f"target storage already exists: {self.data_root}")
        staging.data_root.replace(self.data_root)
        self._fsync_directory(self.root)
        if backup_manifest is not None:
            self._write_json_atomic(self.root / "manifest.v1.json", backup_manifest)
        self._write_manifest(state)

    def _load_generation_state(self, manifest: dict[str, Any]) -> dict[str, Any]:
        if manifest.get("state_version") != STATE_VERSION:
            raise StoreError(f"unsupported state version: {manifest.get('state_version')}")
        generation = str(manifest.get("generation") or "")
        snapshot_path = self.root / "state-data" / "generations" / generation / "snapshot.json"
        snapshot = self._read_json_object(snapshot_path)
        names = snapshot.get("collections")
        if not isinstance(names, list):
            raise StoreError("legacy snapshot collections must be an array")
        state: dict[str, Any] = {
            "version": manifest["state_version"],
            "created_at": manifest["created_at"],
        }
        for name in names:
            if not isinstance(name, str):
                raise StoreError("legacy collection name must be a string")
            state[name] = self._read_json(snapshot_path.parent / "collections" / f"{name}.json")
        return state

    def _hydrate_legacy_transcripts(self, state: dict[str, Any]) -> None:
        threads = state.get("agent_threads")
        if isinstance(threads, dict):
            hydrated = self.hydrate_agent_threads(list(threads.values()))
            state["agent_threads"] = {str(item.get("thread_id") or ""): item for item in hydrated}
        runs = state.get("dispatch_runs")
        if isinstance(runs, dict):
            hydrated_runs = self.hydrate_dispatch_runs(list(runs.values()))
            state["dispatch_runs"] = {str(item.get("run_id") or ""): item for item in hydrated_runs}

    @staticmethod
    def _embed_legacy_messages(state: dict[str, Any]) -> None:
        messages = {
            str(message.get("message_id") or ""): message
            for message in state.get("messages", [])
            if isinstance(message, dict)
        }
        for entries in state.get("session_logs", {}).values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("kind") != "Message":
                    continue
                message_id = str(entry.get("message_id") or "")
                message = messages.get(message_id)
                if message is not None:
                    entry["message"] = dict(message)

    def _write_manifest(self, state: dict[str, Any]) -> None:
        self._write_json_atomic(
            self.manifest_path,
            {
                "storage_version": STORAGE_VERSION,
                "state_version": STATE_VERSION,
                "created_at": str(state.get("created_at") or ""),
                "data_dir": self.data_root.name,
            },
        )

    def _prepare_data_directories(self) -> None:
        for path in (self.metadata_root, self.runtime_root, self.session_root, self.artifacts_root):
            path.mkdir(parents=True, exist_ok=True)
            self._secure_directory_tree(path, stop=self.root)

    @staticmethod
    def _key_digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError as exc:
            raise StoreError(f"state file is missing: {path}") from exc
        except json.JSONDecodeError as exc:
            raise StoreError(f"invalid state JSON: {path}: {exc}") from exc

    def _read_json_object(self, path: Path) -> dict[str, Any]:
        value = self._read_json(path)
        if not isinstance(value, dict):
            raise StoreError(f"state file must contain an object: {path}")
        return value

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        return (
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")

    def _write_json_atomic_if_changed(self, path: Path, value: Any) -> None:
        payload = self._json_bytes(value)
        try:
            if path.read_bytes() == payload:
                return
        except FileNotFoundError:
            pass
        self._write_bytes_atomic(path, payload)

    def _write_json_atomic(self, path: Path, value: Any) -> None:
        self._write_bytes_atomic(path, self._json_bytes(value))

    def _write_text_atomic(self, path: Path, value: str) -> None:
        self._write_bytes_atomic(path, value.encode("utf-8"))

    def _write_bytes_atomic(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._secure_directory_tree(path.parent, stop=self.root)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{new_id('tmp')}.tmp")
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        tmp.replace(path)
        self._fsync_directory(path.parent)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _secure_directory_tree(path: Path, *, stop: Path) -> None:
        stop = stop.resolve()
        current = path.resolve()
        while current != stop and stop in current.parents:
            current.chmod(0o700)
            current = current.parent


class _SessionDirectoryWriter:
    """Builds a complete Session directory while it is still hidden."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, entries: Iterable[dict[str, Any]]) -> None:
        records = [dict(entry) for entry in entries]
        session_id = str(records[0].get("session_id") or "") if records else ""
        log_root = self.root / "log"
        log_root.mkdir(parents=True, exist_ok=True)
        segments: list[dict[str, Any]] = []
        offset = 0
        while offset < len(records):
            start_seq = int(records[offset].get("seq") or 0)
            path = log_root / f"{start_seq:020d}.aizelog"
            payload = bytearray()
            start_offset = offset
            while offset < len(records):
                entry = records[offset]
                encoded = json.dumps(
                    entry,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                frame = FRAME_HEADER.pack(
                    FRAME_MAGIC,
                    len(encoded),
                    zlib.crc32(encoded) & 0xFFFFFFFF,
                ) + encoded
                if payload and len(payload) + len(frame) > MAX_SEGMENT_BYTES:
                    break
                payload.extend(frame)
                offset += 1
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                written = 0
                while written < len(payload):
                    written += os.write(descriptor, payload[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            segment_records = records[start_offset:offset]
            segments.append(
                {
                    "path": str(path.relative_to(self.root)),
                    "start_seq": start_seq,
                    "end_seq": int(segment_records[-1].get("seq") or 0),
                    "records": len(segment_records),
                    "bytes": len(payload),
                }
            )
        index = {
            "version": 1,
            "session_id": session_id,
            "latest_seq": int(records[-1].get("seq") or 0) if records else 0,
            "record_count": len(records),
            "message_count": sum(1 for entry in records if entry.get("kind") == "Message"),
            "segments": segments,
        }
        index_path = self.root / "log.index.json"
        index_payload = (json.dumps(index, indent=2, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(index_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            written = 0
            while written < len(index_payload):
                written += os.write(descriptor, index_payload[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        for directory in (log_root, self.root):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
