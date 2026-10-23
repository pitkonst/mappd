"""The installation timeline: an event log, the resolver's proposals, message authors, takeover."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "installation_0003"
down_revision: str | Sequence[str] | None = "installation_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    """Add events, proposals, `messages.author` (backfilled) and `jobs.taken_over_by`."""
    op.create_table(
        "installation_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("installation_jobs.id"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", TS, nullable=False, server_default=_now()),
    )
    op.create_index("installation_events_job", "installation_events", ["job_id", "id"])
    op.create_table(
        "installation_proposals",
        sa.Column(
            "id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("installation_jobs.id"), nullable=False),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.Text),
        sa.Column("result", JSONB),
        sa.Column("created_at", TS, nullable=False, server_default=_now()),
        sa.Column("decided_at", TS),
    )
    op.add_column(
        "installation_messages",
        sa.Column("author", sa.Text, nullable=False, server_default="installer"),
    )
    messages = sa.table("installation_messages", sa.column("direction"), sa.column("author"))
    op.execute(messages.update().where(messages.c.direction == "out").values(author="bot"))
    op.add_column("installation_jobs", sa.Column("taken_over_by", sa.Text, nullable=True))


def downgrade() -> None:
    """Drop the timeline's tables and columns."""
    op.drop_column("installation_jobs", "taken_over_by")
    op.drop_column("installation_messages", "author")
    op.drop_table("installation_proposals")
    op.drop_index("installation_events_job", table_name="installation_events")
    op.drop_table("installation_events")
