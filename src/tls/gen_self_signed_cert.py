#!/usr/bin/env python3
"""Generate a self-signed TLS certificate for the AIZE Web UI.

Usage (standalone):
    python3 -m tls.gen_self_signed_cert [--cert CERT_PATH] [--key KEY_PATH] [--days DAYS] [--hosts HOSTNAME ...]

Defaults:
    cert:  $AIZE_RUNTIME_ROOT/tls/server.crt  (fallback: ./.aize-runtime/tls/server.crt)
    key:   $AIZE_RUNTIME_ROOT/tls/server.key
    days:  397
    hosts: localhost 127.0.0.1 (always included; extra hosts added via --hosts)
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import ssl
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CERT_DAYS = 397
MAX_SELF_SIGNED_CERT_DAYS = 397


def _default_runtime_root() -> Path:
    root = os.environ.get("AIZE_ROOT")
    if root:
        base = Path(root)
    else:
        base = Path(__file__).resolve().parents[2]
    runtime = os.environ.get("AIZE_RUNTIME_ROOT")
    if runtime:
        return Path(runtime)
    return base / ".aize-runtime"


def _normalize_host(host: str) -> str:
    host = str(host).strip()
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    if "%" in host:
        host = host.split("%", 1)[0]
    host = host.strip()
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        return host


def _host_is_usable_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not (
        ip.is_unspecified
        or ip.is_multicast
        or ip.is_loopback
        or ip.is_link_local
    )


def _dedupe_hosts(hosts: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in hosts:
        host = _normalize_host(raw)
        if not host or host in seen:
            continue
        seen.add(host)
        result.append(host)
    return result


def discover_local_tls_hosts(*, bind_hosts: list[str] | None = None) -> list[str]:
    """Return likely local DNS names and IPv4/IPv6 addresses for Web UI TLS SANs."""
    hosts: list[str] = []
    for host in bind_hosts or []:
        normalized = _normalize_host(host)
        if normalized and normalized not in {"0.0.0.0", "::"}:
            hosts.append(normalized)

    for name in (socket.gethostname(), socket.getfqdn()):
        if name and name not in {"localhost", "localhost.localdomain"}:
            hosts.append(name)
            if "." not in name:
                hosts.append(f"{name}.local")
        try:
            infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
        except OSError:
            continue
        for info in infos:
            address = info[4][0]
            if _host_is_usable_ip(_normalize_host(address)):
                hosts.append(address)

    try:
        result = subprocess.run(
            ["ip", "-o", "addr", "show", "up"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        result = None
    if result and result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.split()
            for family in ("inet", "inet6"):
                if family in parts:
                    idx = parts.index(family)
                    if idx + 1 < len(parts):
                        address = parts[idx + 1].split("/", 1)[0]
                        if _host_is_usable_ip(_normalize_host(address)):
                            hosts.append(address)

    return _dedupe_hosts(hosts)


def _build_san(extra_hosts: list[str] | None = None) -> str:
    """Build a subjectAltName string covering localhost, 127.0.0.1, and any extra hosts."""
    dns_names = ["localhost"]
    ip_addrs = ["127.0.0.1"]
    for h in (extra_hosts or []):
        h = _normalize_host(h)
        if not h:
            continue
        try:
            ipaddress.ip_address(h)
            if h not in ip_addrs:
                ip_addrs.append(h)
        except ValueError:
            if h not in dns_names:
                dns_names.append(h)
    parts = [f"DNS:{n}" for n in dns_names] + [f"IP:{a}" for a in ip_addrs]
    return "subjectAltName=" + ",".join(parts)


def generate_self_signed_cert(
    cert_path: Path,
    key_path: Path,
    *,
    days: int = DEFAULT_CERT_DAYS,
    cn: str = "localhost",
    extra_hosts: list[str] | None = None,
) -> None:
    """Generate a self-signed cert+key pair using openssl.

    Creates parent directories as needed.  Overwrites existing files.
    Always adds SAN for DNS:localhost and IP:127.0.0.1.
    Pass extra_hosts to include additional DNS names or IP addresses in the SAN.
    """
    days = min(int(days), MAX_SELF_SIGNED_CERT_DAYS)
    cert_path = Path(cert_path)
    key_path = Path(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    san = _build_san(extra_hosts)

    # Try modern openssl (>=1.1.1) first: supports -addext inline
    result = subprocess.run(
        [
            "openssl", "req", "-x509",
            "-newkey", "rsa:2048",
            "-keyout", str(key_path),
            "-out", str(cert_path),
            "-days", str(days),
            "-nodes",
            "-subj", f"/CN={cn}",
            "-addext", san,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        # Fallback: write an openssl config file with SAN extension
        cfg = (
            "[req]\n"
            "distinguished_name=dn\n"
            "x509_extensions=v3_req\n"
            "prompt=no\n"
            "[dn]\n"
            f"CN={cn}\n"
            "[v3_req]\n"
            f"{san}\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".cnf", delete=False) as f:
            f.write(cfg)
            cfg_path = f.name
        try:
            subprocess.run(
                [
                    "openssl", "req", "-x509",
                    "-newkey", "rsa:2048",
                    "-keyout", str(key_path),
                    "-out", str(cert_path),
                    "-days", str(days),
                    "-nodes",
                    "-config", cfg_path,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        finally:
            Path(cfg_path).unlink(missing_ok=True)


def _parse_cert_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)


def certificate_needs_regeneration(
    cert_path: Path,
    key_path: Path,
    *,
    required_hosts: list[str] | None = None,
    max_days: int = MAX_SELF_SIGNED_CERT_DAYS,
) -> bool:
    """Return true when a generated Web UI cert is missing, stale, or too broad."""
    if not Path(cert_path).exists() or not Path(key_path).exists():
        return True
    try:
        decoded = ssl._ssl._test_decode_cert(str(cert_path))  # type: ignore[attr-defined]
    except Exception:
        return True

    try:
        not_before = _parse_cert_datetime(str(decoded["notBefore"]))
        not_after = _parse_cert_datetime(str(decoded["notAfter"]))
    except Exception:
        return True
    if (not_after - not_before).days > max_days:
        return True

    san = {
        _normalize_host(str(value))
        for kind, value in decoded.get("subjectAltName", [])
        if kind in {"DNS", "IP Address"}
    }
    required = {
        _normalize_host(host)
        for host in ["localhost", "127.0.0.1", *(required_hosts or [])]
        if _normalize_host(host)
    }
    return not required.issubset(san)


def main() -> int:
    tls_dir = _default_runtime_root() / "tls"
    parser = argparse.ArgumentParser(description="Generate a self-signed TLS certificate.")
    parser.add_argument("--cert", default=str(tls_dir / "server.crt"), help="Output path for the certificate (PEM)")
    parser.add_argument("--key", default=str(tls_dir / "server.key"), help="Output path for the private key (PEM)")
    parser.add_argument("--days", type=int, default=DEFAULT_CERT_DAYS, help="Certificate validity in days (default: 397)")
    parser.add_argument("--cn", default="localhost", help="Common name (default: localhost)")
    parser.add_argument("--no-auto-hosts", action="store_true",
                        help="Do not add local interface IPv4/IPv6 addresses and host names to the SAN")
    parser.add_argument("--hosts", nargs="*", default=[], metavar="HOST",
                        help="Additional DNS names or IP addresses to add to the SAN (space-separated)")
    args = parser.parse_args()

    cert_path = Path(args.cert)
    key_path = Path(args.key)
    hosts = list(args.hosts)
    if not args.no_auto_hosts:
        hosts.extend(discover_local_tls_hosts())
    generate_self_signed_cert(cert_path, key_path, days=args.days, cn=args.cn, extra_hosts=hosts)
    print(f"cert: {cert_path}")
    print(f"key:  {key_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
