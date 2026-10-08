"""Support chatbot sensor: the help desk of a fictional SaaS company, run by a DecoyAgent.

Run with: uvicorn tripvane_sensors.archetypes.support.app:create_app --factory

GET / serves the chat page and POST /chat takes {conversation_id, message}. Each
conversation is one session: its first message is preceded by a SessionStarted, and
every message is an InputReceived on channel http that the DecoyAgent answers. A
per-IP limit of RATE_LIMIT messages per hour keeps a scanner from spending the daily
token budget. GET /health reports the sensor id and archetype.
"""

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from tripvane_core.config import Settings
from tripvane_core.events import EndReason, InputReceived, ModelTurn, SessionEnded, SessionStarted
from tripvane_core.hashing import payload_hash
from tripvane_sensors.archetypes.common import (
    RateLimiter,
    anthropic_client,
    background,
    client_source,
    spooled_sink,
    trusted_proxy_address,
)
from tripvane_sensors.runtime.budget import Budget
from tripvane_sensors.runtime.canary import Canaries
from tripvane_sensors.runtime.decoy_agent import AnyEvent, DecoyAgent, ModelClient, Sink
from tripvane_sensors.runtime.sink import EventSink
from tripvane_sensors.runtime.tools import STANDARD_TOOLS

ARCHETYPE = "support"
RATE_LIMIT = 20
RATE_WINDOW_SECONDS = 3600.0
# One message is one model input; the cap keeps a single message from spending a large
# share of the daily budget, which is only checked before each call.
MAX_MESSAGE_LENGTH = 4000
# Conversations are tracked in memory. The least recently used one is forgotten beyond
# this many; if it comes back it continues as a new session.
MAX_CONVERSATIONS = 10_000

_HERE = Path(__file__).parent
SYSTEM_PROMPT = _HERE / "system.md"
CHAT_PAGE = (_HERE / "chat.html").read_text(encoding="utf-8")

# What the customer sees when the model produced no text for their message.
FALLBACK_REPLIES: dict[EndReason, str] = {
    "completed": "Thanks, I've passed this to our support team. They reply within a business day.",
    "gate_rejected": "Could you tell me a little more about what you need help with?",
    "budget_exhausted": "Sorry, our assistant is busy right now. Please try again later.",
    "error": "Sorry, something went wrong on our side. Please try again in a few minutes.",
}


class ChatRequest(BaseModel):
    conversation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    message: str = Field(max_length=MAX_MESSAGE_LENGTH)


class ChatReply(BaseModel):
    reply: str


def _now() -> datetime:
    return datetime.now(UTC)


def _new_session_id(conversation_id: str) -> str:
    return f"{conversation_id}-{secrets.token_hex(8)}"


@dataclass
class Conversation:
    conversation_id: str
    session_id: str
    next_seq: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def restart(self) -> None:
        """Continue this conversation as a new session."""
        self.session_id = _new_session_id(self.conversation_id)
        self.next_seq = 0


class Conversations:
    """Maps conversation ids to sessions, keeping the most recently used ones in memory.

    The session id is the conversation id plus a random suffix chosen here, so a client
    cannot pick a session id another sensor already owns, and a conversation that
    continues after a restart or after being forgotten becomes a new session instead of
    reusing event sequence numbers the collector has already stored.
    """

    def __init__(self, max_size: int = MAX_CONVERSATIONS) -> None:
        self.max_size = max_size
        self._items: OrderedDict[str, Conversation] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, conversation_id: str) -> Conversation:
        with self._lock:
            conversation = self._items.pop(conversation_id, None)
            if conversation is None:
                conversation = Conversation(conversation_id, _new_session_id(conversation_id))
            self._items[conversation_id] = conversation
            while len(self._items) > self.max_size:
                self._items.popitem(last=False)
            return conversation


def reply_text(events: list[AnyEvent]) -> str:
    """The last text the model wrote in this run, or a fallback for how the run ended."""
    for event in reversed(events):
        if isinstance(event, ModelTurn) and event.assistant_text.strip():
            return event.assistant_text
    ended = events[-1]
    reason: EndReason = ended.reason if isinstance(ended, SessionEnded) else "error"
    return FALLBACK_REPLIES[reason]


def create_app(
    settings: Settings | None = None,
    *,
    client: ModelClient | None = None,
    sink: Sink | None = None,
    canaries: Canaries | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = _now,
) -> FastAPI:
    """Build the sensor app. With no arguments everything comes from the environment.

    Tests pass a mock model client and an in-memory sink. Otherwise events go to an
    EventSink spooling under SPOOL_DIR, shipped to the collector by a background thread.
    """
    if settings is None:
        settings = Settings.from_env()
    if settings.sensor_id is None:
        raise RuntimeError("SENSOR_ID is not set")
    sensor_id = settings.sensor_id
    trusted_proxy = trusted_proxy_address(settings)
    budget = Budget.from_settings(settings)

    shipper: EventSink | None = None
    if sink is None:
        shipper = spooled_sink(settings)
        sink = shipper
    if client is None:
        client = anthropic_client(settings)

    agent = DecoyAgent(
        SYSTEM_PROMPT, STANDARD_TOOLS, client, sink, budget, canaries=canaries, now=now
    )
    limiter = RateLimiter(RATE_LIMIT, RATE_WINDOW_SECONDS, clock)
    conversations = Conversations()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if shipper is None:
            yield
            return
        with background(shipper.ship_forever):
            yield

    # No generated API docs: the page should look like a help desk, not a FastAPI app.
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return CHAT_PAGE

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"sensor_id": sensor_id, "archetype": ARCHETYPE}

    @app.post("/chat")
    def chat(body: ChatRequest, request: Request) -> ChatReply:
        source = client_source(request, trusted_proxy)
        if not limiter.allow(str(source.ip)):
            raise HTTPException(status_code=429, detail="Too many messages")
        conversation = conversations.get(body.conversation_id)
        with conversation.lock:
            stamp = {"sensor_id": sensor_id, "session_id": conversation.session_id}
            if conversation.next_seq == 0:
                sink.emit(SessionStarted(**stamp, event_seq=0, ts=now(), source=source))
                conversation.next_seq = 1
            message = InputReceived(
                **stamp,
                event_seq=conversation.next_seq,
                ts=now(),
                source=source,
                raw_text=body.message,
                channel="http",
                payload_hash=payload_hash(body.message),
            )
            try:
                events = agent.run(conversation.session_id, message)
            except Exception:
                # Part of this run may already be spooled; never reuse its sequence numbers.
                conversation.restart()
                raise
            conversation.next_seq = events[-1].event_seq + 1
        return ChatReply(reply=reply_text(events))

    return app
