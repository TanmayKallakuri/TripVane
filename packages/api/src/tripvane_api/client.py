"""The client address of a request, shared by the canary endpoint and the rate limits."""

import ipaddress

from fastapi import Request

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse_ip(value: str | None) -> IPAddress | None:
    """The address in value, with IPv4-mapped IPv6 addresses unwrapped; None if invalid."""
    if not value:
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def client_ip(
    request: Request, trusted_proxy: IPAddress | None, client_ip_header: str | None
) -> IPAddress | None:
    """The client address, or None when it is unknown.

    With client_ip_header set, it is the first address in that header. Set it only behind a
    platform proxy that writes the header on every request and overwrites any value the
    client sent (Vercel's X-Real-IP); otherwise a client could pick its own address.
    Without it, it is the peer address, unless the peer is trusted_proxy: then it is the
    last X-Forwarded-For entry, the one our proxy appended.
    """
    if client_ip_header is not None:
        address = parse_ip(request.headers.get(client_ip_header, "").split(",", 1)[0])
        if address is not None:
            return address
    peer = parse_ip(request.client.host if request.client else None)
    if peer is None:
        return None
    if trusted_proxy is not None and peer == trusted_proxy:
        forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
        return parse_ip(forwarded.rsplit(",", 1)[-1]) or peer
    return peer
