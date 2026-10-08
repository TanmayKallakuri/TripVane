"""GET /c/{token}: record a hit on a canary URL.

Every request gets the same empty 404, whether the token is a canary or not, so the
response never tells the caller anything. A known token records a canary_hit with the
request's source block; an unknown one records nothing.
"""

import ipaddress
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Request, Response
from sqlalchemy import Engine, insert, select

from tripvane_core.events import Source
from tripvane_core.models import Canary, CanaryHit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def canary_router(
    engine: Engine, trusted_proxy: IPAddress | None, now: Callable[[], datetime]
) -> APIRouter:
    router = APIRouter()

    # Not in the OpenAPI schema: the endpoint should not announce what it is.
    @router.get("/c/{token}", include_in_schema=False)
    def canary_url(token: str, request: Request) -> Response:
        source = request_source(request, trusted_proxy)
        with engine.begin() as conn:
            canary_id = conn.scalar(select(Canary.id).where(Canary.token == token))
            if canary_id is not None and source is not None:
                conn.execute(
                    insert(CanaryHit).values(
                        canary_id=canary_id, ts=now(), source=source.model_dump(mode="json")
                    )
                )
        return Response(status_code=404)

    return router


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


def request_source(request: Request, trusted_proxy: IPAddress | None) -> Source | None:
    """The source block for a request, or None when the client address is unknown.

    The client IP is the peer address, unless the peer is trusted_proxy: then it is the
    last X-Forwarded-For entry, the one our proxy appended. Referer is kept because for a
    canary URL it says where the planted document was opened.
    """
    peer = parse_ip(request.client.host if request.client else None)
    if peer is None:
        return None
    ip = peer
    if trusted_proxy is not None and peer == trusted_proxy:
        forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
        ip = parse_ip(forwarded.rsplit(",", 1)[-1]) or peer
    headers = {
        name: value
        for name in ("Accept-Language", "Referer")
        if (value := request.headers.get(name))
    }
    return Source(ip=ip, user_agent=request.headers.get("user-agent"), headers_subset=headers)
