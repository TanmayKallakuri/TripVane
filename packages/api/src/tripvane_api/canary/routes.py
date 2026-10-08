"""GET /c/{token}: record a hit on a canary URL.

Every request gets the same empty 404, whether the token is a canary or not, so the
response never tells the caller anything. A known token records a canary_hit with the
request's source block; an unknown one records nothing.
"""

from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Request, Response
from sqlalchemy import Engine, insert, select

from tripvane_api.client import IPAddress, client_ip
from tripvane_core.events import Source
from tripvane_core.models import Canary, CanaryHit


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


def request_source(request: Request, trusted_proxy: IPAddress | None) -> Source | None:
    """The source block for a request, or None when the client address is unknown.

    The client IP comes from client_ip (X-Forwarded-For only from trusted_proxy). Referer
    is kept because for a canary URL it says where the planted document was opened.
    """
    ip = client_ip(request, trusted_proxy)
    if ip is None:
        return None
    headers = {
        name: value
        for name in ("Accept-Language", "Referer")
        if (value := request.headers.get(name))
    }
    return Source(ip=ip, user_agent=request.headers.get("user-agent"), headers_subset=headers)
