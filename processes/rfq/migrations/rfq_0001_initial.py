"""RFQ requests: the request, the decision on it, and the reply that went out."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "rfq_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = ("rfq",)
depends_on: str | Sequence[str] | None = "mappd_0001"


def upgrade() -> None:
    """Create the requests table."""
    op.create_table(
        "rfq_requests",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("sender", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="new"),
        sa.Column("output", JSONB),
        sa.Column("decision", JSONB),
        sa.Column("reply", sa.Text),
        sa.Column("approved_by", sa.Text),
        sa.Column("outcome", sa.Text, sa.Computed("output->>'outcome'", persisted=True)),
        sa.Column("escalate_to", sa.Text, sa.Computed("output->>'escalate_to'", persisted=True)),
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


def downgrade() -> None:
    """Drop the requests table."""
    op.drop_table("rfq_requests")
