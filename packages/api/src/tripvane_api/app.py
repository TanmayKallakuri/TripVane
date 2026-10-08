"""Public API: the lookup API and feed (milestone 7) and the canary URL endpoint.

Run with: uvicorn tripvane_api.app:create_app --factory, as one worker, because the
daily request limits are counted in memory (limits.py).
"""

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine

from tripvane_api.canary.routes import canary_router
from tripvane_api.client import parse_ip
from tripvane_api.limits import DailyLimiter
from tripvane_api.lookup.routes import lookup_router
from tripvane_core.config import Settings
from tripvane_core.db import make_engine


def _now() -> datetime:
    return datetime.now(UTC)


def create_app(
    engine: Engine | None = None,
    settings: Settings | None = None,
    *,
    now: Callable[[], datetime] = _now,
) -> FastAPI:
    if settings is None:
        settings = Settings.from_env()
    if engine is None:
        if settings.database_url is None:
            raise RuntimeError("DATABASE_URL is not set")
        engine = make_engine(settings.database_url)
    trusted_proxy = parse_ip(settings.trusted_proxy)
    if settings.trusted_proxy is not None and trusted_proxy is None:
        raise ValueError("TRUSTED_PROXY must be an IP address")
    client_ip_header = settings.client_ip_header
    if trusted_proxy is not None and client_ip_header is not None:
        raise ValueError("set TRUSTED_PROXY or CLIENT_IP_HEADER, not both")

    app = FastAPI(
        title="Tripvane lookup API",
        description=(
            "What the Tripvane deception grid has seen of an IP address, a domain or a "
            "payload. seen false means only that the grid has not seen the value."
        ),
        version="1",
    )
    # web/lookup.html calls the API from any origin, including a file opened from disk.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["X-Api-Key"],
        expose_headers=["Retry-After"],
    )
    # On app.state so tests can reach the counts without making thousands of requests.
    app.state.limiter = DailyLimiter(now)
    app.include_router(
        lookup_router(engine, app.state.limiter, trusted_proxy, client_ip_header, now)
    )
    app.include_router(canary_router(engine, trusted_proxy, client_ip_header, now))
    return app
