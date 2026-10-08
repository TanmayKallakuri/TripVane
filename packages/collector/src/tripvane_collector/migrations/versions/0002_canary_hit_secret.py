"""canary_hits.secret: the canary secret a collector hook hit was found by

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08 09:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("canary_hits", schema=None) as batch_op:
        batch_op.add_column(sa.Column("secret", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("canary_hits", schema=None) as batch_op:
        batch_op.drop_column("secret")
