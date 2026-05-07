"""Tests for the SSRF URL-validation helper in the ingestion service.

These tests do not use the mocked-network autouse fixture from
`test_ingestion_url.py`; they call `_validate_public_url` directly and
monkeypatch `socket.getaddrinfo` to simulate whatever a hostname would
resolve to, so the behaviour is deterministic regardless of the test
environment's DNS.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

import pytest

from journal.services.ingestion.service import _validate_public_url

if TYPE_CHECKING:
    from collections.abc import Iterable


def _fake_getaddrinfo(ip: str) -> object:
    """Return a getaddrinfo stub that always resolves to a single IP."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET

    def _stub(
        _host: str, _port: object, *_args: object, **_kwargs: object
    ) -> Iterable[tuple]:
        return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _stub


def _fake_multi_addrs(ips: list[str]) -> object:
    """Resolver stub that returns multiple addresses (e.g. dual-stack)."""

    def _stub(
        _host: str, _port: object, *_args: object, **_kwargs: object
    ) -> Iterable[tuple]:
        results = []
        for ip in ips:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (
                (ip, 0, 0, 0) if family == socket.AF_INET6 else (ip, 0)
            )
            results.append(
                (family, socket.SOCK_STREAM, 6, "", sockaddr)
            )
        return results

    return _stub


class TestBlockedSchemes:
    def test_file_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="scheme must be http"):
            _validate_public_url("file:///etc/passwd")

    def test_gopher_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="scheme must be http"):
            _validate_public_url("gopher://example.com/")

    def test_ftp_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="scheme must be http"):
            _validate_public_url("ftp://example.com/")

    def test_no_hostname_rejected(self) -> None:
        with pytest.raises(ValueError, match="no hostname"):
            _validate_public_url("http:///path")


class TestBlockedAddresses:
    def test_loopback_ipv4_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1")
        )
        with pytest.raises(ValueError, match="non-public"):
            _validate_public_url("http://localhost/")

    def test_loopback_ipv6_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo("::1")
        )
        with pytest.raises(ValueError, match="non-public"):
            _validate_public_url("http://localhost/")

    def test_link_local_cloud_metadata_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 169.254.169.254 is the EC2/GCP/Azure cloud metadata endpoint
        # — the canonical SSRF target.
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo("169.254.169.254")
        )
        with pytest.raises(ValueError, match="non-public"):
            _validate_public_url("http://metadata.google.internal/")

    def test_private_rfc1918_10_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.5")
        )
        with pytest.raises(ValueError, match="non-public"):
            _validate_public_url("http://internal.example.com/")

    def test_private_rfc1918_192_168_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo("192.168.1.1")
        )
        with pytest.raises(ValueError, match="non-public"):
            _validate_public_url("http://router.local/")

    def test_private_rfc1918_172_16_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo("172.16.0.1")
        )
        with pytest.raises(ValueError, match="non-public"):
            _validate_public_url("http://internal.corp/")

    def test_multicast_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo("224.0.0.1")
        )
        with pytest.raises(ValueError, match="non-public"):
            _validate_public_url("http://multicast.test/")

    def test_unspecified_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo("0.0.0.0")
        )
        with pytest.raises(ValueError, match="non-public"):
            _validate_public_url("http://zero.test/")

    def test_dual_stack_with_private_v6_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A public IPv4 paired with a private IPv6 must fail — we
        # refuse if ANY resolved address is non-public, because the
        # socket layer may prefer the private one.
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_multi_addrs(["93.184.216.34", "fc00::1"])
        )
        with pytest.raises(ValueError, match="non-public"):
            _validate_public_url("http://mixed.test/")


class TestAllowedAddresses:
    def test_public_ipv4_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34")
        )
        # Does not raise.
        _validate_public_url("https://example.com/file.jpg")

    def test_public_ipv6_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo("2606:2800:220:1::1")
        )
        _validate_public_url("https://example.com/file.jpg")

    def test_https_scheme_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34")
        )
        _validate_public_url("https://example.com/")

    def test_http_scheme_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34")
        )
        _validate_public_url("http://example.com/")


class TestResolutionFailure:
    def test_dns_failure_surfaces_as_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_args: object, **_kwargs: object) -> None:
            raise OSError("Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", _fail)
        with pytest.raises(ValueError, match="Failed to resolve"):
            _validate_public_url("http://nonexistent.invalid/")
