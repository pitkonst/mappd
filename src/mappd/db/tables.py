"""Table metadata for the core schema; the source of truth is the migrations.

Keep the definitions in sync with mappd/db/versions — a test fails when they drift.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

METADATA = sa.MetaData()
_NOW = sa.text("now()")

ACTIONS = sa.Table(
    "actions",
    METADATA,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="pending"),
    sa.Column("result", JSONB),
    sa.Column("last_error", sa.Text),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("claimed_by", sa.Text),
    sa.Column("claimed_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
    sa.Column("settled_by", sa.Text),  # who settled it, when it was unknown
    sa.Index("actions_kind_status", "kind", "status"),
)

USERS = sa.Table(
    "mappd_users",
    METADATA,
    sa.Column("name", sa.Text, primary_key=True),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("roles", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
)

MAIL_CURSORS = sa.Table(
    "mappd_mail_cursors",
    METADATA,
    sa.Column("mailbox", sa.Text, primary_key=True),
    sa.Column("uidvalidity", sa.BigInteger),
    sa.Column("last_uid", sa.BigInteger, nullable=False),
    sa.Column("polled_at", sa.DateTime(timezone=True)),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
)
