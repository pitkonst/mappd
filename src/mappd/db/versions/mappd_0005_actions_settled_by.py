"""Who settled an unknown action: the person who checked with the receiver."""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "mappd_0005"
down_revision: str | Sequence[str] | None = "mappd_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `actions.settled_by`; null for actions nobody had to settle."""
    op.add_column("actions", sa.Column("settled_by", sa.Text, nullable=True))


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("actions", "settled_by")
