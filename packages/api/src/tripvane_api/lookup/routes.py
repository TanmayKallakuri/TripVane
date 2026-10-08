"""GET /v1/lookup/{ip,domain,payload}/... and GET /v1/feed/recent.

Lookups are public: 50 requests per UTC day per client IP, or 5,000 per day per API key
when a valid X-Api-Key header is sent. The feed needs a key and counts against it. An
X-Api-Key header that is not a valid, unrevoked key is refused with 401 rather than
treated as anonymous, so a mistyped key is noticed. Every request that passes the key
check counts, including one whose value turns out to be invalid.
"""

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import APIKeyHeader
from sqlalchemy import Engine

from tripvane_api.client import IPAddress, client_ip, parse_ip
from tripvane_api.keys import key_id_for
from tripvane_api.limits import (
    KEY_DAILY_LIMIT,
    PUBLIC_DAILY_LIMIT,
    DailyLimiter,
    ip_client,
    key_client,
)
from tripvane_api.lookup.queries import (
    Feed,
    LookupResult,
    lookup_domain,
    lookup_ip,
    lookup_payload,
    recent_attacks,
)
from tripvane_core.domains import normalize_domain

_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)


def lookup_router(
    engine: Engine,
    limiter: DailyLimiter,
    trusted_proxy: IPAddress | None,
    now: Callable[[], datetime],
) -> APIRouter:
    router = APIRouter(prefix="/v1")

    def api_key_id(key: Annotated[str | None, Depends(_api_key_header)]) -> int | None:
        if key is None:
            return None
        with engine.connect() as conn:
            key_id = key_id_for(conn, key)
        if key_id is None:
            raise HTTPException(status_code=401, detail="invalid API key")
        return key_id

    KeyId = Annotated[int | None, Depends(api_key_id)]

    def count_request(request: Request, key_id: int | None) -> None:
        if key_id is not None:
            client, limit = key_client(key_id), KEY_DAILY_LIMIT
        else:
            ip = client_ip(request, trusted_proxy)
            client, limit = (ip_client(ip) if ip else "ip:unknown"), PUBLIC_DAILY_LIMIT
        if not limiter.allow(client, limit):
            raise HTTPException(
                status_code=429,
                detail="daily request limit reached",
                headers={"Retry-After": str(limiter.seconds_until_reset())},
            )

    @router.get("/lookup/ip/{ip}", tags=["lookup"])
    def get_ip(ip: str, request: Request, key_id: KeyId) -> LookupResult:
        """What the grid has seen from an IPv4 or IPv6 address."""
        count_request(request, key_id)
        # IPv4-mapped IPv6 addresses are unwrapped, as the sensors record them.
        address = parse_ip(ip)
        if address is None:
            raise HTTPException(status_code=422, detail="not an IP address")
        with engine.connect() as conn:
            return lookup_ip(conn, str(address))

    @router.get("/lookup/domain/{domain}", tags=["lookup"])
    def get_domain(domain: str, request: Request, key_id: KeyId) -> LookupResult:
        """What the grid has seen about a domain found in captured inputs and tool calls.

        The match is exact, after lowercasing and IDNA encoding: a subdomain is a
        different domain."""
        count_request(request, key_id)
        normalized = normalize_domain(domain)
        if normalized is None:
            raise HTTPException(status_code=422, detail="not a domain name")
        with engine.connect() as conn:
            return lookup_domain(conn, normalized)

    @router.get("/lookup/payload/{sha256}", tags=["lookup"])
    def get_payload(sha256: str, request: Request, key_id: KeyId) -> LookupResult:
        """What the grid has seen of a payload, by the sha256 of its normalized text:
        lowercased, runs of whitespace collapsed to one space, both ends stripped."""
        count_request(request, key_id)
        if not _SHA256.fullmatch(sha256):
            raise HTTPException(status_code=422, detail="not a 64-character hex sha256")
        with engine.connect() as conn:
            return lookup_payload(conn, sha256.lower())

    @router.get("/feed/recent", tags=["feed"])
    def get_recent(request: Request, key_id: KeyId) -> Feed:
        """Attack payloads seen in the last 24 hours, with their tags and campaign ids.
        Requires an API key."""
        if key_id is None:
            raise HTTPException(status_code=401, detail="API key required")
        count_request(request, key_id)
        with engine.connect() as conn:
            return recent_attacks(conn, now() - timedelta(hours=24))

    return router
