import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tls.gen_self_signed_cert import (
    certificate_needs_regeneration,
    discover_local_tls_hosts,
    generate_self_signed_cert,
)


class TlsCertificateTests(unittest.TestCase):
    def test_generates_browser_compatible_ipv4_ipv6_sans(self):
        with tempfile.TemporaryDirectory() as td:
            cert = Path(td) / "server.crt"
            key = Path(td) / "server.key"
            generate_self_signed_cert(
                cert,
                key,
                extra_hosts=[
                    "debian.local",
                    "192.0.2.10",
                    "2001:db8::10",
                ],
            )

            decoded = ssl._ssl._test_decode_cert(str(cert))  # type: ignore[attr-defined]
            sans = set(decoded.get("subjectAltName", []))
            self.assertIn(("DNS", "localhost"), sans)
            self.assertIn(("DNS", "debian.local"), sans)
            self.assertIn(("IP Address", "127.0.0.1"), sans)
            self.assertIn(("IP Address", "192.0.2.10"), sans)
            self.assertFalse(certificate_needs_regeneration(
                cert,
                key,
                required_hosts=["debian.local", "192.0.2.10", "2001:db8::10"],
            ))

    def test_requested_long_lived_cert_is_capped(self):
        with tempfile.TemporaryDirectory() as td:
            cert = Path(td) / "server.crt"
            key = Path(td) / "server.key"
            generate_self_signed_cert(cert, key, days=3650)

            decoded = ssl._ssl._test_decode_cert(str(cert))  # type: ignore[attr-defined]
            self.assertIn("2027", decoded["notAfter"])
            self.assertFalse(certificate_needs_regeneration(cert, key))

    def test_missing_required_host_is_marked_for_regeneration(self):
        with tempfile.TemporaryDirectory() as td:
            cert = Path(td) / "server.crt"
            key = Path(td) / "server.key"
            generate_self_signed_cert(cert, key)

            self.assertTrue(certificate_needs_regeneration(
                cert,
                key,
                required_hosts=["203.0.113.20"],
            ))

    @mock.patch("tls.gen_self_signed_cert.subprocess.run")
    @mock.patch("tls.gen_self_signed_cert.socket.getfqdn", return_value="webbox")
    @mock.patch("tls.gen_self_signed_cert.socket.gethostname", return_value="webbox")
    @mock.patch("tls.gen_self_signed_cert.socket.getaddrinfo", side_effect=OSError)
    def test_discovers_bind_and_interface_hosts_without_wildcards(
        self,
        _getaddrinfo,
        _gethostname,
        _getfqdn,
        subprocess_run,
    ):
        subprocess_run.return_value = mock.Mock(
            returncode=0,
            stdout=(
                "2: eth0 inet 192.0.2.40/24 brd 192.0.2.255 scope global eth0\\n"
                "2: eth0 inet6 2001:db8::40/64 scope global dynamic eth0\\n"
            ),
        )
        self.assertEqual(
            discover_local_tls_hosts(bind_hosts=["0.0.0.0", "::", "192.0.2.40"]),
            ["192.0.2.40", "webbox", "webbox.local", "2001:db8::40"],
        )


if __name__ == "__main__":
    unittest.main()
