"""One ticket per email: a unique index on the Message-ID its mail record keeps."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "support_0004"
down_revision: str | Sequence[str] | None = "support_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Index mail->>'message_id', so a mail stored under an older key never comes in twice."""
    op.create_index(
        "support_tickets_message_id",
        "support_tickets",
        [sa.text("(mail->>'message_id')")],
        unique=True,
    )


def downgrade() -> None:
    """Drop the index."""
    op.drop_index("support_tickets_message_id", table_name="support_tickets")
