"""analyst: gate verdict and campaign columns on payloads, tag_proposals, analyst_batches

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08 09:36:33.331734
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analyst_batches",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=64), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analyst_batches")),
    )
    op.create_table(
        "tag_proposals",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "payload_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("taxonomy_version", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["payload_id"], ["payloads.id"], name=op.f("fk_tag_proposals_payload_id_payloads")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tag_proposals")),
        sa.UniqueConstraint(
            "payload_id",
            "taxonomy_version",
            name=op.f("uq_tag_proposals_payload_id_taxonomy_version"),
        ),
    )
    with op.batch_alter_table("payloads", schema=None) as batch_op:
        batch_op.add_column(sa.Column("is_attack", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("gate_version", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("campaign_id", sa.BigInteger(), nullable=True))
        batch_op.create_index(batch_op.f("ix_payloads_campaign_id"), ["campaign_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("payloads", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_payloads_campaign_id"))
        batch_op.drop_column("campaign_id")
        batch_op.drop_column("gate_version")
        batch_op.drop_column("is_attack")

    op.drop_table("tag_proposals")
    op.drop_table("analyst_batches")
