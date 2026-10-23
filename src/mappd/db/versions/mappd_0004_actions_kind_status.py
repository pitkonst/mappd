"""Index the outbox by kind and status: the webhook sweep and the operations view scan them."""

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "mappd_0004"
down_revision: str | Sequence[str] | None = "mappd_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the (kind, status) index on actions."""
    op.create_index("actions_kind_status", "actions", ["kind", "status"])


def downgrade() -> None:
    """Drop the index."""
    op.drop_index("actions_kind_status", table_name="actions")
