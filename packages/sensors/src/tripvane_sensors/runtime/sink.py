"""The event sink: every event is spooled to disk first, then shipped to the collector.

emit() appends one JSON line to spool_dir/pending.jsonl and fsyncs before returning, so
an event survives a crash the moment it is emitted. flush() moves the pending file to an
outbox file and posts its lines in batches to COLLECTOR_URL/ingest with the bearer token,
retrying with exponential backoff. If the collector stays unreachable the unsent lines
stay on disk and the next flush() resumes from them; events are never dropped. A batch
the collector can never accept (400, 409, 413, 422 and the like) is moved to a rejected-*.jsonl file
for a human to inspect.
"""

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Self

import httpx2

from tripvane_core.config import Settings
from tripvane_sensors.runtime.decoy_agent import AnyEvent

log = logging.getLogger(__name__)

PENDING = "pending.jsonl"
# Worth retrying: the collector is down, overloaded, or misconfigured in a way an operator
# can fix (auth, routing). Any other non-2xx status means this batch can never be accepted.
RETRY_STATUSES = frozenset({401, 403, 404, 408, 429})


class CollectorNotConfigured(RuntimeError):
    """COLLECTOR_URL or COLLECTOR_TOKEN is missing, so nothing can be shipped."""


class BatchRejected(Exception):
    """The collector answered with a status that means the batch will never be accepted."""


class EventSink:
    def __init__(
        self,
        spool_dir: Path,
        collector_url: str | None = None,
        collector_token: str | None = None,
        *,
        http_client: httpx2.Client | None = None,
        batch_size: int = 100,
        max_attempts: int = 5,
        base_delay: float = 0.5,
        max_delay: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.spool_dir = spool_dir
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.collector_url = collector_url
        self._token = collector_token
        self._http = http_client if http_client is not None else httpx2.Client(timeout=10.0)
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleep
        self._emit_lock = threading.Lock()
        self._flush_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings, spool_dir: Path) -> Self:
        return cls(spool_dir, settings.collector_url, settings.collector_token)

    @property
    def pending_path(self) -> Path:
        return self.spool_dir / PENDING

    def emit(self, event: AnyEvent) -> None:
        """Append the event to the spool and make sure it is on disk before returning."""
        line = event.model_dump_json() + "\n"
        with self._emit_lock, self.pending_path.open("a", encoding="utf-8") as spool:
            spool.write(line)
            spool.flush()
            os.fsync(spool.fileno())

    def flush(self) -> bool:
        """Ship everything spooled so far. Returns True when nothing is left to send."""
        if not self.collector_url or not self._token:
            raise CollectorNotConfigured("COLLECTOR_URL and COLLECTOR_TOKEN must both be set")
        with self._flush_lock:
            self._rotate_pending()
            for outbox in self._outboxes():
                if not self._ship_file(outbox):
                    return False
            return True

    def ship_forever(self, stop: threading.Event, interval: float = 5.0) -> None:
        """Flush every interval seconds until stop is set, then flush once more."""
        while not stop.wait(interval):
            self._flush_logged()
        self._flush_logged()

    def _flush_logged(self) -> None:
        try:
            if not self.flush():
                log.warning("collector unreachable; events stay spooled in %s", self.spool_dir)
        except CollectorNotConfigured:
            log.error("collector is not configured; events stay spooled in %s", self.spool_dir)

    def _rotate_pending(self) -> None:
        # New events go to a fresh pending file while the old one is shipped.
        with self._emit_lock:
            if self.pending_path.exists() and self.pending_path.stat().st_size > 0:
                self.pending_path.rename(self.spool_dir / f"outbox-{time.time_ns()}.jsonl")

    def _outboxes(self) -> list[Path]:
        return sorted(self.spool_dir.glob("outbox-*.jsonl"), key=lambda p: _ns(p.name))

    def _ship_file(self, outbox: Path) -> bool:
        lines = [line for line in outbox.read_text(encoding="utf-8").splitlines() if line]
        while lines:
            batch = lines[: self.batch_size]
            try:
                sent = self._post_with_retry(batch)
            except BatchRejected as exc:
                rejected = self.spool_dir / f"rejected-{time.time_ns()}.jsonl"
                rejected.write_text("\n".join(batch) + "\n", encoding="utf-8")
                log.error("collector rejected a batch (%s); kept in %s", exc, rejected.name)
                sent = True
            if not sent:
                _rewrite(outbox, lines)
                return False
            lines = lines[len(batch) :]
        outbox.unlink()
        return True

    def _post_with_retry(self, batch: list[str]) -> bool:
        body = "[" + ",".join(batch) + "]"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        url = f"{str(self.collector_url).rstrip('/')}/ingest"
        delay = self.base_delay
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._http.post(url, content=body, headers=headers)
            except httpx2.TransportError as exc:
                log.warning("ingest attempt %d failed: %s", attempt, exc)
            else:
                status = response.status_code
                if 200 <= status < 300:
                    return True
                if status < 500 and status not in RETRY_STATUSES:
                    raise BatchRejected(f"HTTP {status}")
                log.warning("ingest attempt %d got HTTP %d", attempt, status)
            if attempt < self.max_attempts:
                self._sleep(delay)
                delay = min(delay * 2, self.max_delay)
        return False


def _ns(name: str) -> int:
    return int(name.removeprefix("outbox-").removesuffix(".jsonl"))


def _rewrite(path: Path, lines: list[str]) -> None:
    """Replace path's contents with lines atomically, so a crash never loses either copy."""
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
