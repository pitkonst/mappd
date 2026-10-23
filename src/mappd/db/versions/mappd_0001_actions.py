"""The actions outbox: one row per side effect, keyed by its idempotency key.

The intent is committed before acting; a re-run reads the row to learn what happened.
"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "mappd_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = ("mappd",)
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the actions table."""
    op.create_table(
        "actions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("result", JSONB),
        sa.Column("last_error", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("claimed_by", sa.Text),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("idempotency_key"),
        sa.CheckConstraint("status IN ('pending', 'sending', 'done', 'failed', 'unknown')"),
    )


def downgrade() -> None:
    """Drop the actions table."""
    op.drop_table("actions")
