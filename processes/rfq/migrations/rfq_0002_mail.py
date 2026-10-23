"""Requests that came by mail keep their email side: ids, attachments, the quote sent."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "rfq_0002"
down_revision: str | Sequence[str] | None = "rfq_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the mail record (a MailInfo as JSON); null for requests from the API."""
    op.add_column("rfq_requests", sa.Column("mail", JSONB, nullable=True))


def downgrade() -> None:
    """Drop the mail record."""
    op.drop_column("rfq_requests", "mail")
