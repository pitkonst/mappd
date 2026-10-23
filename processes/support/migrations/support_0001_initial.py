"""Support tickets and the knowledge base: business state lives here, not in DBOS."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "support_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = ("support",)
depends_on: str | Sequence[str] | None = "mappd_0001"


def upgrade() -> None:
    """Create the tickets and knowledge-base tables."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("sender", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="new"),
        sa.Column("output", JSONB),
        sa.Column("reply", sa.Text),
        sa.Column("approved_by", sa.Text),
        sa.Column("outcome", sa.Text, sa.Computed("output->>'outcome'", persisted=True)),
        sa.Column(
            "escalate_to",
            sa.Text,
            sa.Computed("output->>'escalate_to'", persisted=True),
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
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
        sa.UniqueConstraint("external_id"),
    )
    op.create_table(
        "support_kb",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("embedding", Vector(64), nullable=False),
    )


def downgrade() -> None:
    """Drop the tables."""
    op.drop_table("support_kb")
    op.drop_table("support_tickets")
