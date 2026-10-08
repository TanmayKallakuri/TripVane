"""Pieces every HTTP sensor archetype shares: where a request came from, a per-address
rate limit, the event sink wiring, and sessions that end after a period of inactivity.
"""

import ipaddress
import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anthropic
from fastapi import HTTPException, Request

from tripvane_core.config import Settings
from tripvane_core.events import SessionEnded, SessionStarted
from tripvane_core.events import Source as EventSource
from tripvane_sensors.runtime.decoy_agent import AnyEvent, Sink
from tripvane_sensors.runtime.egress import install_egress_guard
from tripvane_sensors.runtime.sink import EventSink

DEFAULT_SPOOL_DIR = "/var/spool/tripvane"
# A session that has seen no request for this long is over.
IDLE_TIMEOUT_SECONDS = 600.0
REAP_INTERVAL_SECONDS = 30.0
# Instead of the SDK's ten minutes, so a stalled model call does not hold a worker thread.
MODEL_TIMEOUT_SECONDS = 30.0

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
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


def trusted_proxy_address(settings: Settings) -> IPAddress | None:
    """TRUSTED_PROXY as an address; a value that is not an address is a configuration error."""
    address = parse_ip(settings.trusted_proxy)
    if settings.trusted_proxy is not None and address is None:
        raise ValueError("TRUSTED_PROXY must be an IP address")
    if address is not None and settings.client_ip_header is not None:
        raise ValueError("set TRUSTED_PROXY or CLIENT_IP_HEADER, not both")
    return address


def guard_egress(settings: Settings) -> None:
    """Install the in-process egress allowlist when EGRESS_ALLOWED_HOSTS is set.

    Only for hosts with no network-level egress rule (runtime/egress.py). The collector
    host must be on the list, or no event could ever be shipped.
    """
    if settings.egress_allowed_hosts is None:
        return
    hosts = frozenset(
        host.strip().lower() for host in settings.egress_allowed_hosts.split(",") if host.strip()
    )
    collector = urlsplit(settings.collector_url or "").hostname
    if collector is None or collector not in hosts:
        raise ValueError("EGRESS_ALLOWED_HOSTS must include the host of COLLECTOR_URL")
    install_egress_guard(hosts)


def from_this_host(request: Request) -> bool:
    """True for a request from inside the sensor's own container, such as its healthcheck."""
    peer = parse_ip(request.client.host if request.client else None)
    return peer is not None and peer.is_loopback


def client_source(
    request: Request, trusted_proxy: IPAddress | None, client_ip_header: str | None
) -> EventSource:
    """The source block for a request: client IP, user agent and Accept-Language.

    With client_ip_header set, the client IP is the first address in that header; set it
    only behind a platform proxy that writes the header on every request and overwrites
    any value the client sent. Otherwise it is the peer address, unless the peer is
    trusted_proxy: then it is the last X-Forwarded-For entry, the one our proxy appended.
    X-Forwarded-For from any other peer is ignored, because a client can write anything
    into it.
    """
    peer = parse_ip(request.client.host if request.client else None)
    ip = None
    if client_ip_header is not None:
        ip = parse_ip(request.headers.get(client_ip_header, "").split(",", 1)[0])
    if ip is None:
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


def spooled_sink(settings: Settings) -> EventSink:
    """The EventSink a deployed sensor uses: spooled under SPOOL_DIR, shipped to the collector."""
    if settings.collector_url is None or settings.collector_token is None:
        raise RuntimeError("COLLECTOR_URL and COLLECTOR_TOKEN must be set")
    return EventSink.from_settings(settings, Path(settings.spool_dir or DEFAULT_SPOOL_DIR))


def anthropic_client(settings: Settings) -> anthropic.Anthropic:
    if settings.anthropic_api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=MODEL_TIMEOUT_SECONDS)


@contextmanager
def background(*workers: Callable[[threading.Event], None]) -> Iterator[None]:
    """Run each worker(stop) in a daemon thread; on exit set stop and wait for them."""
    stop = threading.Event()
    threads = [threading.Thread(target=worker, args=(stop,), daemon=True) for worker in workers]
    for thread in threads:
        thread.start()
    try:
        yield
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=30)


class TrackedSession:
    """One session's identity and its next event sequence number.

    Events for a session are stamped and emitted only while holding its lock, so their
    sequence numbers are unique and in order.
    """

    def __init__(self, session_id: str, source: EventSource) -> None:
        self.session_id = session_id
        self.source = source
        self.next_seq = 0
        self.lock = threading.Lock()
        self.in_flight = 0
        self.last_seen = 0.0
        self.ended = False


class SessionTracker:
    """Sessions keyed by something the archetype chooses, ended after IDLE_TIMEOUT_SECONDS.

    A session starts with a SessionStarted before its first recorded event and ends with
    a SessionEnded when it is closed, when no request has been in flight for the idle
    timeout (reap), or when the sensor stops (close_all). A session in which nothing was
    recorded emits nothing.

    The tracker is also a Sink: DecoyAgent emits through it, so a session's sequence
    continues after an agent run however that run ended.
    """

    def __init__(
        self,
        sensor_id: str,
        sink: Sink,
        *,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.sensor_id = sensor_id
        self.idle_timeout = idle_timeout
        self._sink = sink
        self._clock = clock
        self._now = now
        self._sessions: dict[str, TrackedSession] = {}
        self._by_id: dict[str, TrackedSession] = {}
        self._lock = threading.Lock()

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._sessions

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def acquire(
        self, key: str, source: EventSource, session_id: str | None = None
    ) -> TrackedSession:
        """The session for key, created if there is none, marked as in use until release().

        A new session gets session_id, or a random id when none is given.
        """
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = TrackedSession(session_id or secrets.token_hex(16), source)
                self._sessions[key] = session
                self._by_id[session.session_id] = session
            session.in_flight += 1
            session.last_seen = self._clock()
            return session

    def release(self, session: TrackedSession) -> None:
        with self._lock:
            session.in_flight -= 1
            session.last_seen = self._clock()

    def stamp(self, session: TrackedSession, source: EventSource) -> dict[str, Any]:
        """Identity, next sequence number, time and source for the session's next event.

        The caller holds session.lock. The first stamp of a session emits its
        SessionStarted.
        """
        session.source = source
        if session.next_seq == 0:
            self.emit(SessionStarted(**self._stamp(session, source)))
        return self._stamp(session, source)

    def emit(self, event: AnyEvent) -> None:
        self._sink.emit(event)
        session = self._by_id.get(event.session_id)
        if session is not None and event.event_seq >= session.next_seq:
            session.next_seq = event.event_seq + 1

    def close(self, key: str) -> None:
        """End the session for key now, if there is one."""
        with self._lock:
            session = self._sessions.pop(key, None)
        if session is not None:
            self._end(session)

    def reap(self) -> int:
        """End every session idle for the timeout. Returns how many were ended."""
        now = self._clock()
        with self._lock:
            idle = [
                key
                for key, session in self._sessions.items()
                if session.in_flight == 0 and now - session.last_seen >= self.idle_timeout
            ]
            ended = [self._sessions.pop(key) for key in idle]
        for session in ended:
            self._end(session)
        return len(ended)

    def close_all(self) -> None:
        with self._lock:
            ended = list(self._sessions.values())
            self._sessions.clear()
        for session in ended:
            self._end(session)

    def reap_forever(self, stop: threading.Event, interval: float = REAP_INTERVAL_SECONDS) -> None:
        """Reap every interval seconds until stop is set, then end every open session."""
        while not stop.wait(interval):
            try:
                self.reap()
            except Exception:
                logger.exception("ending idle sessions failed")
        self.close_all()

    def _stamp(self, session: TrackedSession, source: EventSource) -> dict[str, Any]:
        return {
            "sensor_id": self.sensor_id,
            "session_id": session.session_id,
            "event_seq": session.next_seq,
            "ts": self._now(),
            "source": source,
        }

    def _end(self, session: TrackedSession) -> None:
        with session.lock:
            if not session.ended and session.next_seq > 0:
                self.emit(SessionEnded(**self._stamp(session, session.source), reason="completed"))
            session.ended = True
            with self._lock:
                self._by_id.pop(session.session_id, None)
