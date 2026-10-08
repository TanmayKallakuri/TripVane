"""GitHub issue-triage sensor: the webhook receiver of a triage bot on a public repository.

Run with: uvicorn tripvane_sensors.archetypes.github.app:create_app --factory

POST /webhook receives the deliveries of a GitHub App installed on a public repository
we control. Every delivery must carry an X-Hub-Signature-256 that matches
GITHUB_WEBHOOK_SECRET, or it is answered 401 and recorded nowhere. issues.opened,
issue_comment.created and pull_request.opened are handled; other deliveries are
acknowledged and ignored.

A handled delivery is answered 202 at once, then becomes one session in the
background: a SessionStarted and an InputReceived on channel github, whose raw_text is
the title, the body and, for a pull request, the diff fetched with the App's read-only
token, capped at MAX_TEXT_LENGTH characters. The DecoyAgent answers it with the standard
decoy tools plus add_label and post_comment, which are decoys too. Nothing here ever
writes to GitHub. GET /health reports the sensor id and archetype.
"""

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, Request, Response

from tripvane_core.config import Settings
from tripvane_core.events import InputReceived, SessionEnded, SessionStarted
from tripvane_core.events import Source as EventSource
from tripvane_core.hashing import payload_hash
from tripvane_sensors.archetypes.common import (
    RateLimiter,
    anthropic_client,
    background,
    client_source,
    spooled_sink,
    trusted_proxy_address,
)
from tripvane_sensors.archetypes.github.diff import DiffSource, GitHubApp
from tripvane_sensors.archetypes.github.tools import GITHUB_TOOLS
from tripvane_sensors.runtime.budget import Budget
from tripvane_sensors.runtime.canary import Canaries
from tripvane_sensors.runtime.decoy_agent import DecoyAgent, ModelClient, Sink
from tripvane_sensors.runtime.sink import EventSink

ARCHETYPE = "github"
SYSTEM_PROMPT = Path(__file__).parent / "system.md"
MAX_TEXT_LENGTH = 20_000
# GitHub caps webhook payloads at 25 MB; the three handled events are far smaller.
MAX_BODY_BYTES = 5 * 1024 * 1024
# Model runs per author per hour, as for the support sensor's per-IP limit: every
# delivery comes from GitHub's addresses, so the author is what a flood has in common.
RATE_LIMIT = 20
RATE_WINDOW_SECONDS = 3600.0
HANDLED_EVENTS = {
    ("issues", "opened"),
    ("issue_comment", "created"),
    ("pull_request", "opened"),
}
_DELIVERY_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def signature_is_valid(secret: str, body: bytes, header: str | None) -> bool:
    """True if header is sha256=<hex HMAC-SHA256 of body under secret>."""
    if header is None or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(
        header.removeprefix("sha256=").encode("ascii", "replace"), expected.encode("ascii")
    )


@dataclass(frozen=True)
class Delivery:
    """The parts of a handled webhook delivery the sensor records."""

    author: str | None
    title: str
    body: str
    repository: str
    number: int
    installation_id: int | None
    is_pull_request: bool


def parse_delivery(event: str, payload: Any) -> Delivery | None:  # noqa: ANN401
    """The delivery's content, or None if the event and action are not handled."""
    if not isinstance(payload, dict) or (event, payload.get("action")) not in HANDLED_EVENTS:
        return None
    if event == "pull_request":
        item = payload["pull_request"]
        content = item
    else:
        item = payload["issue"]
        content = payload["comment"] if event == "issue_comment" else item
    installation = payload.get("installation") or {}
    return Delivery(
        author=(content.get("user") or {}).get("login"),
        title=item.get("title") or "",
        body=content.get("body") or "",
        repository=payload["repository"]["full_name"],
        number=int(item["number"]),
        installation_id=installation.get("id"),
        is_pull_request=event == "pull_request",
    )


def assemble_text(title: str, body: str, diff: str | None = None) -> str:
    """Title, body and diff, separated by blank lines, cut at MAX_TEXT_LENGTH."""
    parts = [part for part in (title, body, diff) if part]
    return "\n\n".join(parts)[:MAX_TEXT_LENGTH]


def create_app(
    settings: Settings | None = None,
    *,
    client: ModelClient | None = None,
    sink: Sink | None = None,
    canaries: Canaries | None = None,
    diff_source: DiffSource | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = _now,
) -> FastAPI:
    """Build the sensor app. With no arguments everything comes from the environment.

    Tests pass a mock model client, an in-memory sink and a fake diff source.
    """
    if settings is None:
        settings = Settings.from_env()
    if settings.sensor_id is None:
        raise RuntimeError("SENSOR_ID is not set")
    if settings.github_webhook_secret is None:
        raise RuntimeError("GITHUB_WEBHOOK_SECRET is not set")
    sensor_id = settings.sensor_id
    webhook_secret = settings.github_webhook_secret
    trusted_proxy = trusted_proxy_address(settings)
    budget = Budget.from_settings(settings)

    shipper: EventSink | None = None
    if sink is None:
        shipper = spooled_sink(settings)
        sink = shipper
    if client is None:
        client = anthropic_client(settings)
    if diff_source is None:
        diff_source = GitHubApp.from_settings(settings)

    agent = DecoyAgent(
        SYSTEM_PROMPT, GITHUB_TOOLS, client, sink, budget, canaries=canaries, now=now
    )
    limiter = RateLimiter(RATE_LIMIT, RATE_WINDOW_SECONDS, clock)

    def fetch_diff(delivery: Delivery) -> str | None:
        if delivery.installation_id is None:
            logger.warning("pull request delivery without an installation id")
            return None
        try:
            return diff_source.pull_request_diff(
                int(delivery.installation_id),
                delivery.repository,
                delivery.number,
                MAX_TEXT_LENGTH,
            )
        except Exception:
            # The title and body are still worth recording without the diff.
            logger.exception(
                "fetching the diff of %s#%d failed", delivery.repository, delivery.number
            )
            return None

    def record(delivery: Delivery, source: EventSource, session_id: str) -> None:
        """Turn one delivery into a session; runs after the response has gone out."""
        diff = fetch_diff(delivery) if delivery.is_pull_request else None
        text = assemble_text(delivery.title, delivery.body, diff)
        stamp = {"sensor_id": sensor_id, "session_id": session_id, "source": source}
        sink.emit(SessionStarted(**stamp, event_seq=0, ts=now()))
        event = InputReceived(
            **stamp,
            event_seq=1,
            ts=now(),
            raw_text=text,
            channel="github",
            payload_hash=payload_hash(text),
        )
        if not limiter.allow(delivery.author or str(source.ip)):
            # Recorded, but the model is not called for an author over the limit.
            sink.emit(event)
            sink.emit(SessionEnded(**stamp, event_seq=2, ts=now(), reason="gate_rejected"))
            return
        try:
            agent.run(session_id, event)
        except Exception:
            logger.exception("decoy agent run failed")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if shipper is None:
            yield
            return
        with background(shipper.ship_forever):
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"sensor_id": sensor_id, "archetype": ARCHETYPE}

    @app.post("/webhook")
    async def webhook(
        request: Request,
        tasks: BackgroundTasks,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
    ) -> Response:
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                return Response(status_code=413)
        if not signature_is_valid(webhook_secret, bytes(body), x_hub_signature_256):
            return Response(status_code=401)
        try:
            payload = json.loads(body)
            delivery = parse_delivery(x_github_event or "", payload)
        except (ValueError, KeyError, TypeError, AttributeError):
            return Response(status_code=400)
        if delivery is None:
            return Response(status_code=204)
        source = client_source(request, trusted_proxy).model_copy(
            update={"account_handle": delivery.author}
        )
        delivery_id = x_github_delivery or ""
        prefix = delivery_id if _DELIVERY_ID.match(delivery_id) else "delivery"
        # A random suffix: GitHub redelivers with the same id, and a redelivery must not
        # reuse event sequence numbers the collector has already stored.
        session_id = f"{prefix}-{secrets.token_hex(8)}"
        tasks.add_task(record, delivery, source, session_id)
        return Response(status_code=202)

    return app
