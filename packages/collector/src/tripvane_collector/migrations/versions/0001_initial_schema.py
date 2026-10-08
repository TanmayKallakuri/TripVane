"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-08 07:55:25.472622
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "canaries",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_canaries")),
        sa.UniqueConstraint("token", name=op.f("uq_canaries_token")),
    )
    op.create_table(
        "domains",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_domains")),
        sa.UniqueConstraint("domain", name=op.f("uq_domains_domain")),
    )
    op.create_table(
        "payloads",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seen_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payloads")),
        sa.UniqueConstraint("payload_hash", name=op.f("uq_payloads_payload_hash")),
    )
    op.create_table(
        "sensors",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("archetype", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sensors")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sensors_token_hash")),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("ip", sa.String(length=45), nullable=False),
        sa.Column("asn", sa.BigInteger(), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("ip", name=op.f("uq_sources_ip")),
    )
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("axis", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tags")),
        sa.UniqueConstraint("axis", "name", name=op.f("uq_tags_axis_name")),
    )
    op.create_table(
        "canary_hits",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("canary_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["canary_id"], ["canaries.id"], name=op.f("fk_canary_hits_canary_id_canaries")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_canary_hits")),
    )
    op.create_table(
        "payload_tags",
        sa.Column(
            "payload_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["payload_id"], ["payloads.id"], name=op.f("fk_payload_tags_payload_id_payloads")
        ),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], name=op.f("fk_payload_tags_tag_id_tags")),
        sa.PrimaryKeyConstraint(
            "payload_id", "tag_id", "taxonomy_version", name=op.f("pk_payload_tags")
        ),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("sensor_id", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["sensor_id"], ["sensors.id"], name=op.f("fk_sessions_sensor_id_sensors")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("sensor_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column(
            "payload_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["payload_id"], ["payloads.id"], name=op.f("fk_events_payload_id_payloads")
        ),
        sa.ForeignKeyConstraint(
            ["sensor_id"], ["sensors.id"], name=op.f("fk_events_sensor_id_sensors")
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], name=op.f("fk_events_session_id_sessions")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint(
            "sensor_id",
            "session_id",
            "event_seq",
            name=op.f("uq_events_sensor_id_session_id_event_seq"),
        ),
    )
    op.create_index(op.f("ix_events_payload_id"), "events", ["payload_id"], unique=False)

    op.create_table(
        "session_domains",
        sa.Column(
            "domain_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id"], ["domains.id"], name=op.f("fk_session_domains_domain_id_domains")
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], name=op.f("fk_session_domains_session_id_sessions")
        ),
        sa.PrimaryKeyConstraint("domain_id", "session_id", name=op.f("pk_session_domains")),
    )
    op.create_table(
        "session_sources",
        sa.Column(
            "source_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], name=op.f("fk_session_sources_session_id_sessions")
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_session_sources_source_id_sources")
        ),
        sa.PrimaryKeyConstraint("source_id", "session_id", name=op.f("pk_session_sources")),
    )


def downgrade() -> None:
    op.drop_table("session_sources")
    op.drop_table("session_domains")
    op.drop_index(op.f("ix_events_payload_id"), table_name="events")

    op.drop_table("events")
    op.drop_table("sessions")
    op.drop_table("payload_tags")
    op.drop_table("canary_hits")
    op.drop_table("tags")
    op.drop_table("sources")
    op.drop_table("sensors")
    op.drop_table("payloads")
    op.drop_table("domains")
    op.drop_table("canaries")
