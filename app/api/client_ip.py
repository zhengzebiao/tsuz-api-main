from ipaddress import ip_address, ip_network

from fastapi import Request

from app.core.config import settings


def get_client_ip(request: Request) -> str:
    peer = request.client.host if request.client is not None else "unknown"
    trusted_proxies = _trusted_proxy_networks(settings.trusted_proxy_ips)
    if not _is_trusted(peer, trusted_proxies):
        return peer

    forwarded_for = request.headers.get("x-forwarded-for")
    if not forwarded_for:
        return peer

    forwarded_hosts = [item.strip() for item in forwarded_for.split(",") if item.strip()]
    parsed_hosts = [_valid_ip(item) for item in forwarded_hosts]
    if not parsed_hosts or any(host is None for host in parsed_hosts):
        return peer

    for host in reversed(parsed_hosts):
        if host is not None and not _is_trusted(host, trusted_proxies):
            return host
    return parsed_hosts[0] or peer


def _trusted_proxy_networks(value: str) -> tuple:
    networks = []
    for item in value.split(","):
        item = item.strip()
        if not item or item == "*":
            continue
        try:
            networks.append(ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _is_trusted(host: str, networks: tuple) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in networks)


def _valid_ip(value: str) -> str | None:
    try:
        return str(ip_address(value))
    except ValueError:
        return None
