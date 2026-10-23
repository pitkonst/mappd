"""Support tickets that came by mail keep their email side: ids, attachments, the reply sent."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "support_0003"
down_revision: str | Sequence[str] | None = "support_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the mail record (a MailInfo as JSON); null for tickets from the API or Telegram."""
    op.add_column("support_tickets", sa.Column("mail", JSONB, nullable=True))


def downgrade() -> None:
    """Drop the mail record."""
    op.drop_column("support_tickets", "mail")
