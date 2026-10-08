"""Fixtures for the analyst tests: a SQLite database, fixture payloads and a fake client.

The fake client plays recorded API responses from data/responses/ in place of the models
and the Message Batches API; no test touches the network.
"""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import Message
from anthropic.types.messages import MessageBatch, MessageBatchIndividualResponse
from sqlalchemy import Engine, insert

from tripvane_core.db import make_engine
from tripvane_core.hashing import normalize, payload_hash
from tripvane_core.models import Base, Event, Payload, Sensor, Session
from tripvane_core.taxonomy import Taxonomy, load_taxonomy

DATA = Path(__file__).parent / "data"
RESPONSES = DATA / "responses"
T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def _json(name: str) -> dict[str, Any]:
    return json.loads((RESPONSES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def recorded() -> Callable[[str], Message]:
    """A recorded Messages API response by file name."""
    return lambda name: Message.model_validate(_json(name))


class FakeBatches:
    def __init__(self) -> None:
        self.created: list[list[Any]] = []
        self.status = "in_progress"

    def create(self, *, requests: list[Any]) -> MessageBatch:
        self.created.append(requests)
        return MessageBatch.model_validate(_json("batch_created"))

    def retrieve(self, batch_id: str) -> MessageBatch:
        body = _json("batch_ended" if self.status == "ended" else "batch_created")
        return MessageBatch.model_validate({**body, "id": batch_id})

    def results(self, batch_id: str) -> Iterator[MessageBatchIndividualResponse]:
        lines = (RESPONSES / "batch_results.jsonl").read_text(encoding="utf-8").splitlines()
        return (MessageBatchIndividualResponse.model_validate(json.loads(line)) for line in lines)


class FakeMessages:
    def __init__(self, responses: list[Message | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.batches = FakeBatches()

    def create(self, **request: Any) -> Message:  # noqa: ANN401
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected model call {len(self.requests)}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, responses: list[Message | Exception]) -> None:
        self.messages = FakeMessages(responses)


@pytest.fixture
def fake_client() -> Callable[..., FakeClient]:
    return lambda *responses: FakeClient(list(responses))


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = make_engine(f"sqlite:///{tmp_path / 'analyst.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(Sensor).values(id="support-1", name="s", archetype="support", token_hash="0")
        )
    yield engine
    engine.dispose()


@pytest.fixture
def taxonomy() -> Taxonomy:
    return load_taxonomy()


@pytest.fixture
def payload_texts() -> dict[str, str]:
    return json.loads((DATA / "payloads.json").read_text(encoding="utf-8"))


@pytest.fixture
def add_payload(engine: Engine) -> Callable[..., int]:
    """Store a payload as the collector would: a session, its input event and the payload row.

    Extra keyword arguments set payload columns such as is_attack and gate_version.
    """
    counter = iter(range(1, 1000))

    def add(raw_text: str, *, asn: int | None = None, **columns: Any) -> int:  # noqa: ANN401
        n = next(counter)
        ts = T0 + timedelta(minutes=n)
        session_id = f"session-{n}"
        source = {
            "ip": f"192.0.2.{n}",
            "asn": asn,
            "user_agent": "tripvane-analyst-test",
            "headers_subset": {},
            "account_handle": None,
        }
        event = {
            "sensor_id": "support-1",
            "session_id": session_id,
            "event_seq": 1,
            "ts": ts.isoformat(),
            "source": source,
            "type": "input_received",
            "raw_text": raw_text,
            "channel": "http",
            "payload_hash": payload_hash(raw_text),
        }
        with engine.begin() as conn:
            conn.execute(
                insert(Session).values(id=session_id, sensor_id="support-1", started_at=ts)
            )
            payload_id = conn.execute(
                insert(Payload)
                .values(
                    payload_hash=payload_hash(raw_text),
                    normalized_text=normalize(raw_text),
                    first_seen=ts,
                    last_seen=ts,
                    seen_count=1,
                    **columns,
                )
                .returning(Payload.id)
            ).scalar_one()
            conn.execute(
                insert(Event).values(
                    sensor_id="support-1",
                    session_id=session_id,
                    event_seq=1,
                    type="input_received",
                    ts=ts,
                    payload=event,
                    payload_id=payload_id,
                )
            )
        return payload_id

    return add
