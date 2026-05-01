from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kernel.identity import (
    client_auth_payload,
    ensure_node_identity,
    node_id_from_public_key_b64,
    random_challenge,
    sign_payload,
    verify_signature,
)


@unittest.skipUnless(shutil.which("openssl"), "openssl is required for Ed25519 identity tests")
class NodeIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_node_id_is_public_key_hash(self) -> None:
        identity = ensure_node_identity(self.runtime_root)

        self.assertEqual(identity["node_id"], node_id_from_public_key_b64(identity["public_key"]))
        self.assertTrue(identity["node_id"].startswith("node-"))

    def test_sign_and_verify_client_auth_payload(self) -> None:
        client = ensure_node_identity(self.runtime_root)
        server_root = self.runtime_root / "server"
        server = ensure_node_identity(server_root)
        payload = client_auth_payload(
            client_node_id=client["node_id"],
            client_public_key=client["public_key"],
            client_challenge=random_challenge(),
            server_node_id=server["node_id"],
            server_public_key=server["public_key"],
            server_challenge=random_challenge(),
        )

        signature = sign_payload(self.runtime_root, payload)

        self.assertTrue(verify_signature(client["public_key"], payload, signature))
        self.assertFalse(verify_signature(server["public_key"], payload, signature))


if __name__ == "__main__":
    unittest.main()
