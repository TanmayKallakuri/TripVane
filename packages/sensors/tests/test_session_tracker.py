from datetime import UTC, datetime

from tripvane_core.events import InputReceived
from tripvane_core.events import Source as EventSource
from tripvane_core.hashing import payload_hash
from tripvane_sensors.archetypes.common import IDLE_TIMEOUT_SECONDS, SessionTracker
from tripvane_sensors.runtime.replay import ListSink

SOURCE = EventSource(ip="198.51.100.7")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _tracker() -> tuple[SessionTracker, ListSink, FakeClock]:
    sink, clock = ListSink(), FakeClock()
    tracker = SessionTracker(
        "infra-test", sink, clock=clock, now=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    return tracker, sink, clock


def _record(tracker: SessionTracker, key: str) -> str:
    session = tracker.acquire(key, SOURCE)
    with session.lock:
        tracker.emit(
            InputReceived(
                **tracker.stamp(session, SOURCE),
                raw_text="hello",
                channel="http",
                payload_hash=payload_hash("hello"),
            )
        )
    tracker.release(session)
    return session.session_id


def test_first_event_is_preceded_by_session_started_and_sequence_continues() -> None:
    tracker, sink, _ = _tracker()
    first = _record(tracker, "a")
    second = _record(tracker, "a")
    assert first == second
    assert [event.type for event in sink.events] == [
        "session_started",
        "input_received",
        "input_received",
    ]
    assert [event.event_seq for event in sink.events] == [0, 1, 2]


def test_a_session_with_nothing_recorded_ends_silently() -> None:
    tracker, sink, clock = _tracker()
    tracker.release(tracker.acquire("a", SOURCE))
    clock.now += IDLE_TIMEOUT_SECONDS
    assert tracker.reap() == 1
    assert sink.events == []


def test_a_session_with_a_request_in_flight_is_not_reaped() -> None:
    tracker, sink, clock = _tracker()
    _record(tracker, "a")
    session = tracker.acquire("a", SOURCE)
    clock.now += 10 * IDLE_TIMEOUT_SECONDS
    assert tracker.reap() == 0
    tracker.release(session)
    clock.now += IDLE_TIMEOUT_SECONDS
    assert tracker.reap() == 1
    assert sink.events[-1].type == "session_ended"


def test_close_ends_one_session_and_a_new_one_gets_a_new_id() -> None:
    tracker, sink, _ = _tracker()
    first = _record(tracker, "a")
    tracker.close("a")
    tracker.close("a")
    assert [event.type for event in sink.events].count("session_ended") == 1
    assert _record(tracker, "a") != first
