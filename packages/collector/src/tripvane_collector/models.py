"""Database schema: the milestone 1 tables plus the links frozen at schema review.

Additions over the milestone 1 prompt (see tripvane-reviews/m1-schema-review.md):
events.payload_id, session_sources, domains and session_domains, so later lookups
join on indexed columns instead of reading event JSON.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# SQLite only autoincrements INTEGER PRIMARY KEY, so big ids fall back to Integer there.
BigId = BigInteger().with_variant(Integer(), "sqlite")
Json = JSON().with_variant(JSONB(), "postgresql")
Timestamp = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Sensor(Base):
    __tablename__ = "sensors"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    archetype: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    sensor_id: Mapped[str] = mapped_column(ForeignKey("sensors.id"))
    started_at: Mapped[datetime] = mapped_column(Timestamp)
    ended_at: Mapped[datetime | None] = mapped_column(Timestamp)
    end_reason: Mapped[str | None] = mapped_column(String(32))


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("sensor_id", "session_id", "event_seq"),)

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    sensor_id: Mapped[str] = mapped_column(ForeignKey("sensors.id"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    event_seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(32))
    ts: Mapped[datetime] = mapped_column(Timestamp)
    payload: Mapped[dict[str, Any]] = mapped_column(Json)
    # Set for input_received events: links a payload to its sessions and sensors.
    payload_id: Mapped[int | None] = mapped_column(ForeignKey("payloads.id"), index=True)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    ip: Mapped[str] = mapped_column(String(45), unique=True)
    asn: Mapped[int | None] = mapped_column(BigInteger)
    first_seen: Mapped[datetime] = mapped_column(Timestamp)
    last_seen: Mapped[datetime] = mapped_column(Timestamp)


class SessionSource(Base):
    """Every source IP that sent an event in a session."""

    __tablename__ = "session_sources"

    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)


class Domain(Base):
    """Domains found in URLs and email addresses in inputs and tool call arguments."""

    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    domain: Mapped[str] = mapped_column(String(253), unique=True)
    first_seen: Mapped[datetime] = mapped_column(Timestamp)
    last_seen: Mapped[datetime] = mapped_column(Timestamp)


class SessionDomain(Base):
    __tablename__ = "session_domains"

    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)


class Payload(Base):
    __tablename__ = "payloads"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), unique=True)
    normalized_text: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(Timestamp)
    last_seen: Mapped[datetime] = mapped_column(Timestamp)
    seen_count: Mapped[int] = mapped_column(Integer)


class Canary(Base):
    __tablename__ = "canaries"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    token: Mapped[str] = mapped_column(String(128), unique=True)
    owner_kind: Mapped[str] = mapped_column(String(32))
    owner_id: Mapped[str] = mapped_column(String(128))
    document_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class CanaryHit(Base):
    __tablename__ = "canary_hits"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    # Nullable: milestone 5 records hits for recognised secrets with no matching canary row.
    canary_id: Mapped[int | None] = mapped_column(ForeignKey("canaries.id"))
    ts: Mapped[datetime] = mapped_column(Timestamp)
    source: Mapped[dict[str, Any]] = mapped_column(Json)


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("axis", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    axis: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))


class PayloadTag(Base):
    __tablename__ = "payload_tags"

    payload_id: Mapped[int] = mapped_column(ForeignKey("payloads.id"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"), primary_key=True)
    taxonomy_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    confidence: Mapped[float] = mapped_column(Float)
