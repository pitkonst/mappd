"""Support tickets record the decision on them: who, how, and what they corrected."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "support_0002"
down_revision: str | Sequence[str] | None = "support_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the decision record (a DecisionRecord as JSON)."""
    op.add_column("support_tickets", sa.Column("decision", JSONB, nullable=True))


def downgrade() -> None:
    """Drop the decision record."""
    op.drop_column("support_tickets", "decision")
