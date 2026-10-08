"""Public API. Run with: uvicorn tripvane_api.app:create_app --factory

Milestone 5 serves the canary URL endpoint; the lookup API joins it in milestone 7.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import FastAPI
from sqlalchemy import Engine

from tripvane_api.canary.routes import canary_router, parse_ip
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

    app = FastAPI(title="tripvane api")
    app.include_router(canary_router(engine, trusted_proxy, now))
    return app
