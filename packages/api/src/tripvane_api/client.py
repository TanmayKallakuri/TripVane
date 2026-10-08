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


def client_ip(request: Request, trusted_proxy: IPAddress | None) -> IPAddress | None:
    """The client address, or None when it is unknown.

    It is the peer address, unless the peer is trusted_proxy: then it is the last
    X-Forwarded-For entry, the one our proxy appended.
    """
    peer = parse_ip(request.client.host if request.client else None)
    if peer is None:
        return None
    if trusted_proxy is not None and peer == trusted_proxy:
        forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
        return parse_ip(forwarded.rsplit(",", 1)[-1]) or peer
    return peer
