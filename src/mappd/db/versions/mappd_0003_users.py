"""Users: operators who sign in when MAPPD_AUTH=jwt."""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "mappd_0003"
down_revision: str | Sequence[str] | None = "mappd_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the users table."""
    op.create_table(
        "mappd_users",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("roles", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    """Drop the users table."""
    op.drop_table("mappd_users")
