"""Installation jobs: installers, jobs, their messages, deadlines and the polling offset."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "installation_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = ("installation",)
depends_on: str | Sequence[str] | None = "mappd_0001"

TS = sa.DateTime(timezone=True)


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    """Create the tables."""
    op.create_table(
        "installation_installers",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("link_token", sa.Text),
        sa.Column("chat_id", sa.BigInteger),
        sa.Column("created_at", TS, nullable=False, server_default=_now()),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("link_token"),
        sa.UniqueConstraint("chat_id"),
    )
    op.create_table(
        "installation_jobs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column(
            "installer_id",
            sa.BigInteger,
            sa.ForeignKey("installation_installers.id"),
            nullable=False,
        ),
        sa.Column("site", sa.Text, nullable=False),
        sa.Column("skus", JSONB, nullable=False),
        sa.Column("appointment_at", TS, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="assigned"),
        sa.Column("blocked_from", sa.Text),
        sa.Column("blocked_reason", sa.Text),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", TS, nullable=False, server_default=_now()),
        sa.Column("updated_at", TS, nullable=False, server_default=_now()),
        sa.UniqueConstraint("external_id"),
    )
    op.create_table(
        "installation_messages",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("installation_jobs.id")),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("direction", sa.Text, nullable=False),
        sa.Column("channel_key", sa.Text),
        sa.Column("text", sa.Text),
        sa.Column("photo_ref", sa.Text),
        sa.Column("photo_description", sa.Text),
        sa.Column("created_at", TS, nullable=False, server_default=_now()),
        sa.UniqueConstraint("channel_key"),
    )
    op.create_table(
        "installation_deadlines",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("installation_jobs.id"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("due_at", TS, nullable=False),
        sa.Column("settled_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=_now()),
    )
    op.create_table(
        "installation_polling",
        sa.Column("bot", sa.Text, primary_key=True),
        sa.Column("offset", sa.BigInteger, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Drop the tables."""
    for table in (
        "installation_polling",
        "installation_deadlines",
        "installation_messages",
        "installation_jobs",
        "installation_installers",
    ):
        op.drop_table(table)
