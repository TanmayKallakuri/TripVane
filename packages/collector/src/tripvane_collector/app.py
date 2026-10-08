"""Collector ingest API. Run with: uvicorn tripvane_collector.app:create_app --factory"""

from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy import Engine

from tripvane_collector.auth import sensor_for_token
from tripvane_collector.ingest import IngestError, ingest
from tripvane_core.config import Settings
from tripvane_core.db import make_engine
from tripvane_core.events import Event

_UNAUTHORIZED = HTTPException(
    status_code=401, detail="unknown sensor", headers={"WWW-Authenticate": "Bearer"}
)


def create_app(engine: Engine | None = None, settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings.from_env()
    if engine is None:
        if settings.database_url is None:
            raise RuntimeError("DATABASE_URL is not set")
        engine = make_engine(settings.database_url)
    # Needed to recognise canary secrets in tool calls; checked now, not on the first call.
    canary_key = settings.canary_hmac_key
    if canary_key is None:
        raise RuntimeError("CANARY_HMAC_KEY is not set")

    app = FastAPI(title="tripvane collector")

    def authenticated_sensor(authorization: Annotated[str | None, Header()] = None) -> str:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise _UNAUTHORIZED
        with engine.connect() as conn:
            sensor_id = sensor_for_token(conn, token.strip())
        if sensor_id is None:
            raise _UNAUTHORIZED
        return sensor_id

    @app.post("/ingest")
    def ingest_events(
        events: list[Event], sensor_id: Annotated[str, Depends(authenticated_sensor)]
    ) -> dict[str, int]:
        try:
            with engine.begin() as conn:
                result = ingest(conn, sensor_id, events, canary_key=canary_key)
        except IngestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return {"inserted": result.inserted, "duplicates": result.duplicates}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
