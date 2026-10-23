"""Deadlines record when the check acted, apart from when the deadline was resolved."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "installation_0002"
down_revision: str | Sequence[str] | None = "installation_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add notified_at: set once by the overdue check; settled_at stays for the resolution."""
    op.add_column(
        "installation_deadlines",
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop notified_at."""
    op.drop_column("installation_deadlines", "notified_at")
