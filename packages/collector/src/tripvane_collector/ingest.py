"""Idempotent event ingest.

Each event is keyed on (sensor_id, session_id, event_seq). A duplicate delivery is a
no-op: the event row is not inserted again and its side effects (sources, payloads,
domains, session links, canary hits, session end) are not applied again. All writes use
database upserts so concurrent deliveries of the same batch cannot double count.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Column, ColumnElement, Connection, Table, func, insert, select, update
from sqlalchemy.dialects import postgresql, sqlite

from tripvane_core import models
from tripvane_core.canary_formats import find_canary_secrets
from tripvane_core.domains import extract_domains, extract_domains_from_value
from tripvane_core.events import Event, InputReceived, SessionEnded, ToolCallAttempted
from tripvane_core.hashing import normalize

sessions: Table = models.Session.__table__  # type: ignore[assignment]
events_table: Table = models.Event.__table__  # type: ignore[assignment]
sources: Table = models.Source.__table__  # type: ignore[assignment]
payloads: Table = models.Payload.__table__  # type: ignore[assignment]
session_sources: Table = models.SessionSource.__table__  # type: ignore[assignment]
domains: Table = models.Domain.__table__  # type: ignore[assignment]
session_domains: Table = models.SessionDomain.__table__  # type: ignore[assignment]
canaries: Table = models.Canary.__table__  # type: ignore[assignment]
canary_hits: Table = models.CanaryHit.__table__  # type: ignore[assignment]


class IngestError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class IngestResult:
    inserted: int
    duplicates: int


def ingest(
    conn: Connection, sensor_id: str, events: Sequence[Event], *, canary_key: str
) -> IngestResult:
    """Store a batch for the authenticated sensor. Raises IngestError; caller rolls back.

    canary_key is CANARY_HMAC_KEY, used to recognise canary secrets in tool calls.
    """
    for event in events:
        if event.sensor_id != sensor_id:
            raise IngestError(403, f"event sensor_id {event.sensor_id!r} does not match token")
    inserted = 0
    for event in events:
        _upsert_session(conn, event)
        event_id = _insert_event(conn, event)
        if event_id is None:
            continue
        inserted += 1
        _link_source(conn, event)
        if isinstance(event, InputReceived):
            _link_payload(conn, event, event_id)
            _link_domains(conn, event, extract_domains(event.raw_text))
        elif isinstance(event, ToolCallAttempted):
            _link_domains(conn, event, extract_domains_from_value(event.arguments))
            _record_canary_hits(conn, event, canary_key)
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


def _insert_event(conn: Connection, event: Event) -> int | None:
    """Insert the event and return its row id, or None if it was already stored."""
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
    return conn.execute(stmt).scalar()


def _link_source(conn: Connection, event: Event) -> None:
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
    source_id = conn.execute(stmt.returning(sources.c.id)).scalar_one()
    link = _insert(conn, session_sources).values(source_id=source_id, session_id=event.session_id)
    conn.execute(link.on_conflict_do_nothing())


def _link_payload(conn: Connection, event: InputReceived, event_id: int) -> None:
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
    payload_id = conn.execute(stmt.returning(payloads.c.id)).scalar_one()
    conn.execute(
        update(events_table).where(events_table.c.id == event_id).values(payload_id=payload_id)
    )


def _link_domains(conn: Connection, event: Event, found: set[str]) -> None:
    for domain in sorted(found):
        stmt = _insert(conn, domains).values(domain=domain, first_seen=event.ts, last_seen=event.ts)
        stmt = stmt.on_conflict_do_update(
            index_elements=[domains.c.domain],
            set_={
                "first_seen": _earlier(conn, domains.c.first_seen, stmt.excluded.first_seen),
                "last_seen": _later(conn, domains.c.last_seen, stmt.excluded.last_seen),
            },
        )
        domain_id = conn.execute(stmt.returning(domains.c.id)).scalar_one()
        link = _insert(conn, session_domains).values(
            domain_id=domain_id, session_id=event.session_id
        )
        conn.execute(link.on_conflict_do_nothing())


def _record_canary_hits(conn: Connection, event: ToolCallAttempted, canary_key: str) -> None:
    """One canary_hit per canary secret in the call's arguments.

    The hit is linked to the canaries row whose token is the secret; a secret with a
    valid checksum but no row (a canary minted elsewhere, or planted by hand) is recorded
    with a null canary_id. The secret is stored either way.
    """
    for secret in find_canary_secrets(event.arguments, canary_key):
        canary_id = conn.scalar(select(canaries.c.id).where(canaries.c.token == secret))
        conn.execute(
            insert(canary_hits).values(
                canary_id=canary_id,
                ts=event.ts,
                source=event.source.model_dump(mode="json"),
                secret=secret,
            )
        )


def _end_session(conn: Connection, event: SessionEnded) -> None:
    conn.execute(
        update(sessions)
        .where(sessions.c.id == event.session_id)
        .values(ended_at=event.ts, end_reason=event.reason)
    )
