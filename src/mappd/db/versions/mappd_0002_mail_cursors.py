"""Mail cursors: per mailbox, the IMAP UIDVALIDITY and the last UID read."""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "mappd_0002"
down_revision: str | Sequence[str] | None = "mappd_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the mail cursor table."""
    op.create_table(
        "mappd_mail_cursors",
        sa.Column("mailbox", sa.Text, primary_key=True),
        sa.Column("uidvalidity", sa.BigInteger),
        sa.Column("last_uid", sa.BigInteger, nullable=False),
        sa.Column("polled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    """Drop the mail cursor table."""
    op.drop_table("mappd_mail_cursors")
