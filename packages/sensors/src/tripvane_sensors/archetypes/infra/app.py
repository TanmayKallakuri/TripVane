"""Infrastructure lookalike sensor: static Ollama, Open WebUI, LiteLLM and Langflow endpoints.

Run with: uvicorn tripvane_sensors.archetypes.infra.app:create_app --factory

No model is involved and nothing is executed. Every request, to any path with any
method, is recorded as an InputReceived on channel http whose raw_text is the request
body, or the query string for GET, followed by a ToolCallAttempted named http_request
that carries the method, path and query string (the schema has no other place for
them) with the response body as its fake result. The listed endpoints answer with the
static JSON in responses.py; a listed path with another method answers 405 and any
other path 404.

Requests are grouped into sessions per client address; a session ends after
IDLE_TIMEOUT_SECONDS without a request. There is no /health of the sensor's own:
/health is LiteLLM's, so the container healthcheck only opens a TCP connection.
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from tripvane_core.config import Settings
from tripvane_core.events import InputReceived, ToolCallAttempted
from tripvane_core.events import Source as EventSource
from tripvane_core.hashing import payload_hash
from tripvane_sensors.archetypes.common import (
    IPAddress,
    SessionTracker,
    TrackedSession,
    background,
    client_source,
    guard_egress,
    spooled_sink,
    trusted_proxy_address,
    utc_now,
)
from tripvane_sensors.archetypes.infra.responses import (
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    ROUTES,
    TOO_LARGE,
)
from tripvane_sensors.runtime.decoy_agent import Sink

ARCHETYPE = "infra"
TOOL_NAME = "http_request"
# Bodies are recorded up to this size; a larger request is answered 413 and recorded
# with its body cut at this size.
MAX_BODY_BYTES = 64 * 1024


def route(method: str, path: str) -> tuple[int, Any]:
    """The static status and body for a request."""
    methods = ROUTES.get(path.rstrip("/") or "/")
    if methods is None:
        return 404, NOT_FOUND
    return methods.get(method, (405, METHOD_NOT_ALLOWED))


async def read_body(request: Request, limit: int) -> tuple[bytes, bool]:
    """Up to limit bytes of the body, and whether there was more."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return bytes(body[:limit]), True
    return bytes(body), False


class Lookalike:
    """ASGI app that records every request and answers it from the static routes."""

    def __init__(
        self,
        tracker: SessionTracker,
        trusted_proxy: IPAddress | None,
        client_ip_header: str | None,
    ) -> None:
        self.tracker = tracker
        self.trusted_proxy = trusted_proxy
        self.client_ip_header = client_ip_header

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        source = client_source(request, self.trusted_proxy, self.client_ip_header)
        body, truncated = await read_body(request, MAX_BODY_BYTES)
        method = request.method
        raw_path = scope.get("raw_path")
        path = raw_path.decode("latin-1") if raw_path else scope["path"]
        query = scope.get("query_string", b"").decode("latin-1")
        status, payload = (413, TOO_LARGE) if truncated else route(method, scope["path"])
        # Compact, like the Go and FastAPI servers being imitated.
        response_text = json.dumps(payload, separators=(",", ":"))
        raw_text = query if method == "GET" else body.decode("utf-8", errors="replace")
        request_line = {"method": method, "path": path, "query": query}

        session = self.tracker.acquire(f"ip:{source.ip}", source)
        try:
            await anyio.to_thread.run_sync(
                self._record, session, source, raw_text, request_line, response_text
            )
        finally:
            self.tracker.release(session)
        response = Response(response_text, status_code=status, media_type="application/json")
        await response(scope, receive, send)

    def _record(
        self,
        session: TrackedSession,
        source: EventSource,
        raw_text: str,
        request_line: dict[str, str],
        response_text: str,
    ) -> None:
        with session.lock:
            self.tracker.emit(
                InputReceived(
                    **self.tracker.stamp(session, source),
                    raw_text=raw_text,
                    channel="http",
                    payload_hash=payload_hash(raw_text),
                )
            )
            self.tracker.emit(
                ToolCallAttempted(
                    **self.tracker.stamp(session, source),
                    tool_name=TOOL_NAME,
                    arguments=request_line,
                    fake_result=response_text,
                )
            )


def create_app(
    settings: Settings | None = None,
    *,
    sink: Sink | None = None,
    clock: Callable[[], float] | None = None,
    now: Callable[[], datetime] = utc_now,
) -> Starlette:
    """Build the sensor app. With no arguments everything comes from the environment.

    Tests pass an in-memory sink and a fake clock. The tracker is app.state.tracker.
    """
    if settings is None:
        settings = Settings.from_env()
    if settings.sensor_id is None:
        raise RuntimeError("SENSOR_ID is not set")
    guard_egress(settings)
    trusted_proxy = trusted_proxy_address(settings)
    shipper = None
    if sink is None:
        shipper = spooled_sink(settings)
        sink = shipper
    clock_args = {} if clock is None else {"clock": clock}
    tracker = SessionTracker(settings.sensor_id, sink, now=now, **clock_args)
    shippers = [] if shipper is None else [shipper.ship_forever]

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # The reaper stops first and ends every open session; the shipper then sends that.
        with background(*shippers), background(tracker.reap_forever):
            yield

    app = Starlette(
        routes=[Mount("/", app=Lookalike(tracker, trusted_proxy, settings.client_ip_header))],
        lifespan=lifespan,
    )
    app.state.tracker = tracker
    return app
