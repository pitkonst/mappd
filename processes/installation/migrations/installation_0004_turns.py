"""Turns on the timeline: a reply and the events of a message point at the message."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "installation_0004"
down_revision: str | Sequence[str] | None = "installation_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `messages.reply_to` and `events.message_id`; null for rows of no message's turn."""
    op.add_column(
        "installation_messages",
        sa.Column(
            "reply_to", sa.BigInteger, sa.ForeignKey("installation_messages.id"), nullable=True
        ),
    )
    op.add_column(
        "installation_events",
        sa.Column(
            "message_id", sa.BigInteger, sa.ForeignKey("installation_messages.id"), nullable=True
        ),
    )


def downgrade() -> None:
    """Drop both columns."""
    op.drop_column("installation_events", "message_id")
    op.drop_column("installation_messages", "reply_to")
