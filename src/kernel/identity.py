from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


IDENTITY_DIR_NAME = "identity"
PRIVATE_KEY_NAME = "ed25519_private.pem"
PUBLIC_KEY_NAME = "ed25519_public.pem"
NODE_ID_PREFIX = "node-"


def identity_dir(runtime_root: Path) -> Path:
    return runtime_root / IDENTITY_DIR_NAME


def identity_paths(runtime_root: Path) -> tuple[Path, Path]:
    root = identity_dir(runtime_root)
    return root / PRIVATE_KEY_NAME, root / PUBLIC_KEY_NAME


def _run_openssl(args: list[str], *, input_bytes: bytes | None = None) -> bytes:
    proc = subprocess.run(
        ["openssl", *args],
        input=input_bytes,
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "openssl_failed")
    return proc.stdout


def _public_der_from_private(private_key_path: Path) -> bytes:
    return _run_openssl(["pkey", "-in", str(private_key_path), "-pubout", "-outform", "DER"])


def _public_pem_from_private(private_key_path: Path) -> bytes:
    return _run_openssl(["pkey", "-in", str(private_key_path), "-pubout"])


def node_id_from_public_key_der(public_key_der: bytes) -> str:
    return NODE_ID_PREFIX + hashlib.sha256(public_key_der).hexdigest()


def normalize_public_key_b64(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "-----BEGIN PUBLIC KEY-----" in text:
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.startswith("-----")
        ]
        der = base64.b64decode("".join(lines), validate=True)
        return base64.b64encode(der).decode("ascii")
    der = base64.b64decode(text, validate=True)
    return base64.b64encode(der).decode("ascii")


def node_id_from_public_key_b64(public_key_b64: str) -> str:
    der = base64.b64decode(normalize_public_key_b64(public_key_b64), validate=True)
    return node_id_from_public_key_der(der)


def ensure_node_identity(runtime_root: Path) -> dict[str, str]:
    private_key_path, public_key_path = identity_paths(runtime_root)
    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    if not private_key_path.exists():
        _run_openssl(["genpkey", "-algorithm", "Ed25519", "-out", str(private_key_path)])
        private_key_path.chmod(0o600)
    public_der = _public_der_from_private(private_key_path)
    public_pem = _public_pem_from_private(private_key_path)
    public_key_path.write_bytes(public_pem)
    public_key_path.chmod(0o644)
    public_key_b64 = base64.b64encode(public_der).decode("ascii")
    node_id = node_id_from_public_key_der(public_der)
    state_path = runtime_root / "state" / "node_id"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(node_id + "\n", encoding="utf-8")
    return {
        "node_id": node_id,
        "public_key": public_key_b64,
        "private_key_path": str(private_key_path),
        "public_key_path": str(public_key_path),
    }


def sign_payload(runtime_root: Path, payload: str) -> str:
    private_key_path, _public_key_path = identity_paths(runtime_root)
    if not private_key_path.exists():
        ensure_node_identity(runtime_root)
    with tempfile.NamedTemporaryFile() as input_file, tempfile.NamedTemporaryFile() as sig_file:
        input_file.write(payload.encode("utf-8"))
        input_file.flush()
        _run_openssl(
            [
                "pkeyutl",
                "-sign",
                "-inkey",
                str(private_key_path),
                "-rawin",
                "-in",
                input_file.name,
                "-out",
                sig_file.name,
            ]
        )
        return base64.b64encode(Path(sig_file.name).read_bytes()).decode("ascii")


def verify_signature(public_key_b64: str, payload: str, signature_b64: str) -> bool:
    try:
        public_der = base64.b64decode(normalize_public_key_b64(public_key_b64), validate=True)
        signature = base64.b64decode(str(signature_b64 or "").strip(), validate=True)
    except Exception:
        return False
    with (
        tempfile.NamedTemporaryFile() as pub_file,
        tempfile.NamedTemporaryFile() as input_file,
        tempfile.NamedTemporaryFile() as sig_file,
    ):
        public_pem = (
            b"-----BEGIN PUBLIC KEY-----\n"
            + base64.encodebytes(public_der)
            + b"-----END PUBLIC KEY-----\n"
        )
        pub_file.write(public_pem)
        pub_file.flush()
        input_file.write(payload.encode("utf-8"))
        input_file.flush()
        sig_file.write(signature)
        sig_file.flush()
        proc = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                pub_file.name,
                "-rawin",
                "-in",
                input_file.name,
                "-sigfile",
                sig_file.name,
            ],
            check=False,
            capture_output=True,
        )
        return proc.returncode == 0


def random_challenge() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")


def canonical_auth_payload(kind: str, fields: dict[str, Any]) -> str:
    return json.dumps(
        {"kind": kind, "fields": fields},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def server_auth_payload(
    *,
    client_node_id: str,
    client_public_key: str,
    client_challenge: str,
    server_node_id: str,
    server_public_key: str,
    server_challenge: str,
) -> str:
    return canonical_auth_payload(
        "aize.ws.server.v1",
        {
            "client_challenge": client_challenge,
            "client_node_id": client_node_id,
            "client_public_key": normalize_public_key_b64(client_public_key),
            "server_challenge": server_challenge,
            "server_node_id": server_node_id,
            "server_public_key": normalize_public_key_b64(server_public_key),
        },
    )


def client_auth_payload(
    *,
    client_node_id: str,
    client_public_key: str,
    client_challenge: str,
    server_node_id: str,
    server_public_key: str,
    server_challenge: str,
) -> str:
    return canonical_auth_payload(
        "aize.ws.client.v1",
        {
            "client_challenge": client_challenge,
            "client_node_id": client_node_id,
            "client_public_key": normalize_public_key_b64(client_public_key),
            "server_challenge": server_challenge,
            "server_node_id": server_node_id,
            "server_public_key": normalize_public_key_b64(server_public_key),
        },
    )


def public_key_matches_node_id(node_id: str, public_key_b64: str) -> bool:
    try:
        return str(node_id or "").strip() == node_id_from_public_key_b64(public_key_b64)
    except Exception:
        return False
