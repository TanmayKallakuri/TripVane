"""Support chatbot sensor: the help desk of a fictional SaaS company, run by a DecoyAgent.

Run with: uvicorn tripvane_sensors.archetypes.support.app:create_app --factory

GET / serves the chat page and POST /chat takes {conversation_id, message}. Each
conversation is one session: its first message is preceded by a SessionStarted, and
every message is an InputReceived on channel http that the DecoyAgent answers. A
per-IP limit of RATE_LIMIT messages per hour keeps a scanner from spending the daily
token budget. GET /health reports the sensor id and archetype.
"""

import ipaddress
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from tripvane_core.config import Settings
from tripvane_core.events import EndReason, InputReceived, ModelTurn, SessionEnded, SessionStarted
from tripvane_core.events import Source as EventSource
from tripvane_core.hashing import payload_hash
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
DEFAULT_SPOOL_DIR = "/var/spool/tripvane"
MODEL_TIMEOUT_SECONDS = 30.0

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

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class ChatRequest(BaseModel):
    conversation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    message: str = Field(max_length=MAX_MESSAGE_LENGTH)


class ChatReply(BaseModel):
    reply: str


def _now() -> datetime:
    return datetime.now(UTC)


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


def client_source(request: Request, trusted_proxy: IPAddress | None) -> EventSource:
    """The source block for a request: client IP, user agent and Accept-Language.

    The client IP is the peer address, unless the peer is trusted_proxy: then it is the
    last X-Forwarded-For entry, the one our proxy appended. X-Forwarded-For from any
    other peer is ignored, because a client can write anything into it.
    """
    peer = parse_ip(request.client.host if request.client else None)
    if peer is None:
        raise HTTPException(status_code=400, detail="unknown client address")
    ip = peer
    if trusted_proxy is not None and peer == trusted_proxy:
        forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
        ip = parse_ip(forwarded.rsplit(",", 1)[-1]) or peer
    headers = {}
    accept_language = request.headers.get("accept-language")
    if accept_language:
        headers["Accept-Language"] = accept_language
    return EventSource(ip=ip, user_agent=request.headers.get("user-agent"), headers_subset=headers)


class RateLimiter:
    """Sliding window: at most `limit` accepted requests per key in any `window` seconds."""

    def __init__(
        self, limit: int, window: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._sweep_at = 1024

    def allow(self, key: str) -> bool:
        """Record a request for key and return True, or return False if key is over the limit."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > self._sweep_at:
                self._sweep(now)
            return True

    def _sweep(self, now: float) -> None:
        """Forget keys with no request inside the window, so memory stays bounded."""
        self._hits = {
            key: hits for key, hits in self._hits.items() if hits and now - hits[-1] < self.window
        }
        self._sweep_at = max(1024, 2 * len(self._hits))


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
    trusted_proxy = parse_ip(settings.trusted_proxy)
    if settings.trusted_proxy is not None and trusted_proxy is None:
        raise ValueError("TRUSTED_PROXY must be an IP address")
    budget = Budget.from_settings(settings)

    shipper: EventSink | None = None
    if sink is None:
        if settings.collector_url is None or settings.collector_token is None:
            raise RuntimeError("COLLECTOR_URL and COLLECTOR_TOKEN must be set")
        shipper = EventSink.from_settings(settings, Path(settings.spool_dir or DEFAULT_SPOOL_DIR))
        sink = shipper
    if client is None:
        if settings.anthropic_api_key is None:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key, timeout=MODEL_TIMEOUT_SECONDS
        )

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
        stop = threading.Event()
        thread = threading.Thread(target=shipper.ship_forever, args=(stop,), daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=30)

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
