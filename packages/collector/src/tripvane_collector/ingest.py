"""Idempotent event ingest.

Each event is keyed on (sensor_id, session_id, event_seq). A duplicate delivery is a
no-op: the event row is not inserted again and its side effects (sources, payloads,
session end) are not applied again. All writes use database upserts so concurrent
deliveries of the same batch cannot double count.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Column, ColumnElement, Connection, Table, func, update
from sqlalchemy.dialects import postgresql, sqlite

from tripvane_collector import models
from tripvane_core.events import Event, InputReceived, SessionEnded
from tripvane_core.hashing import normalize

sessions: Table = models.Session.__table__  # type: ignore[assignment]
events_table: Table = models.Event.__table__  # type: ignore[assignment]
sources: Table = models.Source.__table__  # type: ignore[assignment]
payloads: Table = models.Payload.__table__  # type: ignore[assignment]


class IngestError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class IngestResult:
    inserted: int
    duplicates: int


def ingest(conn: Connection, sensor_id: str, events: Sequence[Event]) -> IngestResult:
    """Store a batch for the authenticated sensor. Raises IngestError; caller rolls back."""
    for event in events:
        if event.sensor_id != sensor_id:
            raise IngestError(403, f"event sensor_id {event.sensor_id!r} does not match token")
    inserted = 0
    for event in events:
        _upsert_session(conn, event)
        if _insert_event(conn, event):
            inserted += 1
            _upsert_source(conn, event)
            if isinstance(event, InputReceived):
                _upsert_payload(conn, event)
            elif isinstance(event, SessionEnded):
                _end_session(conn, event)
    return IngestResult(inserted=inserted, duplicates=len(events) - inserted)


def _insert(conn: Connection, table: Table) -> postgresql.Insert | sqlite.Insert:
    if conn.dialect.name == "postgresql":
        return postgresql.insert(table)
    if conn.dialect.name == "sqlite":
        return sqlite.insert(table)
    raise NotImplementedError(f"unsupported database dialect {conn.dialect.name}")


def _earlier(conn: Connection, a: Column[datetime], b: Column[datetime]) -> ColumnElement[datetime]:
    return func.least(a, b) if conn.dialect.name == "postgresql" else func.min(a, b)


def _later(conn: Connection, a: Column[datetime], b: Column[datetime]) -> ColumnElement[datetime]:
    return func.greatest(a, b) if conn.dialect.name == "postgresql" else func.max(a, b)


def _upsert_session(conn: Connection, event: Event) -> None:
    """Create the session on its first event; started_at is the earliest event ts seen."""
    stmt = _insert(conn, sessions).values(
        id=event.session_id, sensor_id=event.sensor_id, started_at=event.ts
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[sessions.c.id],
        set_={"started_at": _earlier(conn, sessions.c.started_at, stmt.excluded.started_at)},
        where=sessions.c.sensor_id == stmt.excluded.sensor_id,
    )
    # RETURNING, not rowcount: rowcount is unreliable for upserts across drivers.
    if conn.execute(stmt.returning(sessions.c.id)).first() is None:
        raise IngestError(409, f"session {event.session_id!r} belongs to another sensor")


def _insert_event(conn: Connection, event: Event) -> bool:
    stmt = (
        _insert(conn, events_table)
        .values(
            sensor_id=event.sensor_id,
            session_id=event.session_id,
            event_seq=event.event_seq,
            type=event.type,
            ts=event.ts,
            payload=event.model_dump(mode="json"),
        )
        .on_conflict_do_nothing(index_elements=["sensor_id", "session_id", "event_seq"])
        .returning(events_table.c.id)
    )
    return conn.execute(stmt).first() is not None


def _upsert_source(conn: Connection, event: Event) -> None:
    stmt = _insert(conn, sources).values(
        ip=str(event.source.ip), asn=event.source.asn, first_seen=event.ts, last_seen=event.ts
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[sources.c.ip],
        set_={
            "asn": func.coalesce(stmt.excluded.asn, sources.c.asn),
            "first_seen": _earlier(conn, sources.c.first_seen, stmt.excluded.first_seen),
            "last_seen": _later(conn, sources.c.last_seen, stmt.excluded.last_seen),
        },
    )
    conn.execute(stmt)


def _upsert_payload(conn: Connection, event: InputReceived) -> None:
    stmt = _insert(conn, payloads).values(
        payload_hash=event.payload_hash,
        normalized_text=normalize(event.raw_text),
        first_seen=event.ts,
        last_seen=event.ts,
        seen_count=1,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[payloads.c.payload_hash],
        set_={
            "seen_count": payloads.c.seen_count + 1,
            "first_seen": _earlier(conn, payloads.c.first_seen, stmt.excluded.first_seen),
            "last_seen": _later(conn, payloads.c.last_seen, stmt.excluded.last_seen),
        },
    )
    conn.execute(stmt)


def _end_session(conn: Connection, event: SessionEnded) -> None:
    conn.execute(
        update(sessions)
        .where(sessions.c.id == event.session_id)
        .values(ended_at=event.ts, end_reason=event.reason)
    )
