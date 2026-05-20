from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime import cli_service_adapter


class HttpBindResolutionTests(unittest.TestCase):
    def test_wildcard_host_binds_ipv4_and_ipv6(self) -> None:
        self.assertEqual(
            cli_service_adapter._resolve_bind_specs("0.0.0.0"),
            [("0.0.0.0", socket.AF_INET), ("::", socket.AF_INET6)],
        )

    def test_ipv6_host_preserves_ipv6_bind(self) -> None:
        self.assertEqual(
            cli_service_adapter._resolve_bind_specs("::1"),
            [("::1", socket.AF_INET6)],
        )

    def test_ipv4_host_preserves_ipv4_bind(self) -> None:
        self.assertEqual(
            cli_service_adapter._resolve_bind_specs("127.0.0.1"),
            [("127.0.0.1", socket.AF_INET)],
        )

    def test_restart_wrapper_does_not_hardcode_dispatch_port_4123(self) -> None:
        script = (ROOT / "scripts" / "restart_aize_unit.sh").read_text(encoding="utf-8")
        self.assertIn('AIZE_HTTP_PORT_VALUE="${AIZE_HTTP_PORT_VALUE:-4123}"', script)
        self.assertIn("AIZE_HTTP_PORT='$AIZE_HTTP_PORT_VALUE'", script)
        self.assertNotIn("AIZE_HTTP_PORT='${AIZE_HTTP_PORT:-4123}'", script)

    def test_root_restart_script_terminates_default_http_port_alongside_active_port(self) -> None:
        script = (ROOT / "restart_aize_unit.sh").read_text(encoding="utf-8")
        self.assertIn('DEFAULT_HTTP_PORT="4123"', script)
        self.assertIn('if [[ "$HTTP_PORT" != "$DEFAULT_HTTP_PORT" ]]; then', script)
        self.assertIn('log "port $port owners terminated"', script)

    def test_register_user_script_resolves_runtime_port_and_https(self) -> None:
        script = (ROOT / "scripts" / "register_user.sh").read_text(encoding="utf-8")
        self.assertIn('RUNTIME_ROOT="${AIZE_RUNTIME_ROOT:-$ROOT/.aize-runtime}"', script)
        self.assertIn('runtime_root / "state" / "services.json"', script)
        self.assertIn('BASE_URL="${SCHEME}://${HOST}:${PORT}"', script)
        self.assertIn('if [[ "$SCHEME" == "https" ]]; then', script)
        self.assertIn('curl -sf "${CURL_TLS_ARGS[@]}" -X POST "${BASE_URL}/login"', script)
        self.assertIn('curl -sf "${CURL_TLS_ARGS[@]}" -X POST "${BASE_URL}/register"', script)

    def test_agents_notes_describe_active_httpbridge_health_url(self) -> None:
        notes = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(
            "the active HttpBridge health URL resolved from runtime state (default `https://127.0.0.1:4123/health`)",
            notes,
        )
        self.assertIn(
            "the active HttpBridge health URL should be reachable; the default is `https://127.0.0.1:4123/health`",
            notes,
        )
        self.assertIn(
            "active HttpBridge health URL (default `https://127.0.0.1:4123/health`)",
            notes,
        )


if __name__ == "__main__":
    unittest.main()
