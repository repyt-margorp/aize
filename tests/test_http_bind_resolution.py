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


if __name__ == "__main__":
    unittest.main()
