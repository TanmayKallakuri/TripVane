"""What the grid knows about an IP, a domain or a payload hash.

Every lookup reads the same way: the sessions the value appears in give session_count
and sensor_count; the attack payloads received in those sessions give the tags and the
campaign. Tags are read under each payload's gate_version, the taxonomy version of its
current verdict, so the tags always agree with the verdict and no taxonomy file is needed.
"""

from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import ColumnElement, Connection, Select, func, select

from tripvane_core.models import (
    Domain,
    Event,
    Payload,
    PayloadTag,
    SessionDomain,
    SessionSource,
    Source,
    Tag,
)
from tripvane_core.models import Session as SessionRow

TOP_TAGS = 3


class TagCount(BaseModel):
    tag: str
    # Sessions in which a payload carrying this tag was received.
    count: int


class LookupResult(BaseModel):
    seen: bool
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    sensor_count: int | None = None
    session_count: int | None = None
    # Axis -> up to three tags, most frequent first.
    tags: dict[str, list[TagCount]] | None = None
    campaign_id: int | None = None


class FeedPayload(BaseModel):
    payload_hash: str
    # Axis -> tag; empty until the payload has been tagged.
    tags: dict[str, str]
    campaign_id: int | None


class Feed(BaseModel):
    payloads: list[FeedPayload]


UNSEEN = LookupResult(seen=False)


def lookup_ip(conn: Connection, ip: str) -> LookupResult:
    """ip is the canonical text form of the address (str(ipaddress.ip_address(...)))."""
    row = conn.execute(
        select(Source.id, Source.first_seen, Source.last_seen).where(Source.ip == ip)
    ).first()
    if row is None:
        return UNSEEN
    sessions = select(SessionSource.session_id).where(SessionSource.source_id == row.id)
    return _summarize(conn, row.first_seen, row.last_seen, sessions, Event.session_id.in_(sessions))


def lookup_domain(conn: Connection, domain: str) -> LookupResult:
    """domain is normalized (tripvane_core.domains.normalize_domain)."""
    row = conn.execute(
        select(Domain.id, Domain.first_seen, Domain.last_seen).where(Domain.domain == domain)
    ).first()
    if row is None:
        return UNSEEN
    sessions = select(SessionDomain.session_id).where(SessionDomain.domain_id == row.id)
    return _summarize(conn, row.first_seen, row.last_seen, sessions, Event.session_id.in_(sessions))


def lookup_payload(conn: Connection, sha256: str) -> LookupResult:
    """sha256 is the lowercase payload hash (tripvane_core.hashing.payload_hash)."""
    row = conn.execute(
        select(Payload.id, Payload.first_seen, Payload.last_seen).where(
            Payload.payload_hash == sha256
        )
    ).first()
    if row is None:
        return UNSEEN
    sessions = select(Event.session_id).where(Event.payload_id == row.id)
    # Only this payload's own tags, not those of other inputs in the same sessions.
    return _summarize(conn, row.first_seen, row.last_seen, sessions, Event.payload_id == row.id)


def recent_attacks(conn: Connection, since: datetime) -> Feed:
    """Attack payloads last seen at or after since, most recent first."""
    rows = conn.execute(
        select(Payload.id, Payload.payload_hash, Payload.campaign_id)
        .where(Payload.is_attack.is_(True), Payload.last_seen >= since)
        .order_by(Payload.last_seen.desc(), Payload.payload_hash)
    ).all()
    tags: dict[int, dict[str, str]] = {row.id: {} for row in rows}
    if rows:
        tag_rows = conn.execute(
            select(PayloadTag.payload_id, Tag.axis, Tag.name)
            .join(Tag, Tag.id == PayloadTag.tag_id)
            .join(Payload, Payload.id == PayloadTag.payload_id)
            .where(
                PayloadTag.taxonomy_version == Payload.gate_version,
                PayloadTag.payload_id.in_(list(tags)),
            )
            .order_by(Tag.id)
        )
        for payload_id, axis, name in tag_rows:
            tags[payload_id][axis] = name
    return Feed(
        payloads=[
            FeedPayload(
                payload_hash=row.payload_hash, tags=tags[row.id], campaign_id=row.campaign_id
            )
            for row in rows
        ]
    )


def _summarize(
    conn: Connection,
    first_seen: datetime,
    last_seen: datetime,
    sessions: Select[tuple[str]],
    received: ColumnElement[bool],
) -> LookupResult:
    """sessions selects the session ids the value appears in; received restricts events
    to the inputs whose payloads describe the value."""
    session_count, sensor_count = conn.execute(
        select(
            func.count(SessionRow.id.distinct()), func.count(SessionRow.sensor_id.distinct())
        ).where(SessionRow.id.in_(sessions))
    ).one()
    attack_inputs = (
        select()
        .select_from(Event)
        .join(Payload, Payload.id == Event.payload_id)
        .where(Payload.is_attack.is_(True), received)
    )
    sessions_with = func.count(Event.session_id.distinct())
    tag_rows = conn.execute(
        attack_inputs.add_columns(Tag.axis, Tag.name, sessions_with)
        .join(
            PayloadTag,
            (PayloadTag.payload_id == Payload.id)
            & (PayloadTag.taxonomy_version == Payload.gate_version),
        )
        .join(Tag, Tag.id == PayloadTag.tag_id)
        .group_by(Tag.axis, Tag.name)
    ).all()
    order = _axis_order(conn)
    tags: dict[str, list[TagCount]] = {}
    for axis, name, count in sorted(tag_rows, key=lambda r: (order.get(r[0], 0), -r[2], r[1])):
        top = tags.setdefault(axis, [])
        if len(top) < TOP_TAGS:
            top.append(TagCount(tag=name, count=count))
    # The campaign most of the value's sessions belong to; ties go to the older campaign.
    campaign_id = conn.execute(
        attack_inputs.add_columns(Payload.campaign_id)
        .where(Payload.campaign_id.is_not(None))
        .group_by(Payload.campaign_id)
        .order_by(sessions_with.desc(), Payload.campaign_id)
        .limit(1)
    ).scalar()
    return LookupResult(
        seen=True,
        first_seen=_utc(first_seen),
        last_seen=_utc(last_seen),
        sensor_count=sensor_count,
        session_count=session_count,
        tags=tags,
        campaign_id=campaign_id,
    )


def _axis_order(conn: Connection) -> dict[str, int]:
    """Axes in taxonomy file order: the tagger adds each axis's tags to tags in that order."""
    rows = conn.execute(select(Tag.axis, func.min(Tag.id)).group_by(Tag.axis))
    return {axis: first_id for axis, first_id in rows}


def _utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes; every stored timestamp is UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
