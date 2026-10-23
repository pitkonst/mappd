"""One request per email: a unique index on the Message-ID its mail record keeps."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "rfq_0003"
down_revision: str | Sequence[str] | None = "rfq_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Index mail->>'message_id', so a mail stored under an older key never comes in twice."""
    op.create_index(
        "rfq_requests_message_id", "rfq_requests", [sa.text("(mail->>'message_id')")], unique=True
    )


def downgrade() -> None:
    """Drop the index."""
    op.drop_index("rfq_requests_message_id", table_name="rfq_requests")
