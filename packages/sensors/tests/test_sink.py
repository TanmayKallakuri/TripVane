import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from tripvane_core.events import SessionStarted, Source
from tripvane_sensors.runtime.sink import CollectorNotConfigured, EventSink

COLLECTOR_URL = "http://collector.test"
# Test-only bearer token; not a credential for anything.
TOKEN = "test-token-replay-1"


def _event(seq: int) -> SessionStarted:
    return SessionStarted(
        sensor_id="replay-1",
        session_id=f"session-{seq}",
        event_seq=0,
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        source=Source(ip="192.0.2.1"),
    )


class Collector:
    """A scripted collector behind httpx2.MockTransport. statuses are served in order."""

    def __init__(self, statuses: list[int | Exception]) -> None:
        self.statuses = list(statuses)
        self.received: list[list[dict]] = []
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        outcome = self.statuses.pop(0) if self.statuses else 200
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == 200:
            self.received.append(json.loads(request.content))
        return httpx2.Response(outcome, json={})


def _sink(tmp_path: Path, collector: Collector, **kwargs: int) -> tuple[EventSink, list[float]]:
    delays: list[float] = []
    sink = EventSink(
        tmp_path / "spool",
        COLLECTOR_URL,
        TOKEN,
        http_client=httpx2.Client(transport=httpx2.MockTransport(collector)),
        sleep=delays.append,
        **kwargs,
    )
    return sink, delays


def _spooled_lines(sink: EventSink) -> list[str]:
    lines: list[str] = []
    for path in sorted(sink.spool_dir.glob("*.jsonl")):
        if not path.name.startswith("rejected-"):
            lines += [line for line in path.read_text().splitlines() if line]
    return lines


def test_emit_spools_before_anything_is_shipped(tmp_path: Path) -> None:
    collector = Collector([])
    sink, _ = _sink(tmp_path, collector)
    sink.emit(_event(1))
    sink.emit(_event(2))
    assert collector.requests == []
    lines = sink.pending_path.read_text().splitlines()
    assert [json.loads(line)["session_id"] for line in lines] == ["session-1", "session-2"]


def test_flush_ships_batches_with_the_bearer_token(tmp_path: Path) -> None:
    collector = Collector([])
    sink, _ = _sink(tmp_path, collector, batch_size=2)
    for seq in range(5):
        sink.emit(_event(seq))
    assert sink.flush()
    assert [len(batch) for batch in collector.received] == [2, 2, 1]
    request = collector.requests[0]
    assert str(request.url) == f"{COLLECTOR_URL}/ingest"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert _spooled_lines(sink) == []


def test_retries_with_backoff_then_succeeds(tmp_path: Path) -> None:
    collector = Collector([httpx2.ConnectError("refused"), 503, 429, 200])
    sink, delays = _sink(tmp_path, collector)
    sink.emit(_event(1))
    assert sink.flush()
    assert delays == [0.5, 1.0, 2.0]
    assert len(collector.received) == 1


def test_backoff_is_capped(tmp_path: Path) -> None:
    collector = Collector([500] * 6)
    sink, delays = _sink(tmp_path, collector, max_attempts=6)
    sink.max_delay = 3.0
    sink.emit(_event(1))
    assert not sink.flush()
    assert delays == [0.5, 1.0, 2.0, 3.0, 3.0]


def test_collector_down_keeps_every_event(tmp_path: Path) -> None:
    collector = Collector([httpx2.ConnectError("refused")] * 10)
    sink, _ = _sink(tmp_path, collector)
    sink.emit(_event(1))
    sink.emit(_event(2))
    assert not sink.flush()
    sink.emit(_event(3))
    assert not sink.flush()
    sessions = [json.loads(line)["session_id"] for line in _spooled_lines(sink)]
    assert sorted(sessions) == ["session-1", "session-2", "session-3"]
    assert not list(sink.spool_dir.glob("rejected-*"))


def test_resumes_after_a_partial_failure(tmp_path: Path) -> None:
    # First batch lands, then the collector goes down for the whole second batch.
    collector = Collector([200] + [503] * 5)
    sink, _ = _sink(tmp_path, collector, batch_size=2)
    for seq in range(4):
        sink.emit(_event(seq))
    assert not sink.flush()
    remaining = [json.loads(line)["session_id"] for line in _spooled_lines(sink)]
    assert remaining == ["session-2", "session-3"]
    assert sink.flush()
    shipped = [event["session_id"] for batch in collector.received for event in batch]
    assert shipped == ["session-0", "session-1", "session-2", "session-3"]
    assert _spooled_lines(sink) == []


def test_unacceptable_batch_is_moved_aside_not_dropped(tmp_path: Path) -> None:
    collector = Collector([422, 200])
    sink, delays = _sink(tmp_path, collector, batch_size=1)
    sink.emit(_event(1))
    sink.emit(_event(2))
    assert sink.flush()
    assert delays == []
    rejected = list(sink.spool_dir.glob("rejected-*.jsonl"))
    assert len(rejected) == 1
    assert json.loads(rejected[0].read_text())["session_id"] == "session-1"
    assert collector.received == [[json.loads(_event(2).model_dump_json())]]


def test_flush_requires_collector_settings(tmp_path: Path) -> None:
    sink = EventSink(tmp_path / "spool")
    sink.emit(_event(1))
    with pytest.raises(CollectorNotConfigured):
        sink.flush()
    assert len(_spooled_lines(sink)) == 1
