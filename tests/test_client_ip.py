from starlette.requests import Request

from app.api.client_ip import get_client_ip
from app.core.config import settings


def make_request(peer: str | None, forwarded_for: str | None = None) -> Request:
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/email/register/code",
        "headers": headers,
        "client": (peer, 12345) if peer is not None else None,
    }
    return Request(scope)


def test_direct_request_ignores_spoofed_forwarded_for(monkeypatch) -> None:
    monkeypatch.setattr(settings, "trusted_proxy_ips", "127.0.0.1,::1")

    request = make_request("198.51.100.20", "203.0.113.10")

    assert get_client_ip(request) == "198.51.100.20"


def test_trusted_proxy_chain_returns_first_untrusted_address(monkeypatch) -> None:
    monkeypatch.setattr(settings, "trusted_proxy_ips", "127.0.0.1,10.0.0.0/8")

    request = make_request("127.0.0.1", "203.0.113.10, 10.2.0.8")

    assert get_client_ip(request) == "203.0.113.10"


def test_invalid_forwarded_for_falls_back_to_peer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "trusted_proxy_ips", "127.0.0.1")

    request = make_request("127.0.0.1", "not-an-ip")

    assert get_client_ip(request) == "127.0.0.1"


def test_missing_client_address_uses_unknown(monkeypatch) -> None:
    monkeypatch.setattr(settings, "trusted_proxy_ips", "127.0.0.1")

    assert get_client_ip(make_request(None, "203.0.113.10")) == "unknown"


def test_wildcard_proxy_configuration_is_ignored(monkeypatch) -> None:
    monkeypatch.setattr(settings, "trusted_proxy_ips", "*")

    request = make_request("198.51.100.20", "203.0.113.10")

    assert get_client_ip(request) == "198.51.100.20"
