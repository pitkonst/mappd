"""Table metadata of the support process; the source of truth is the migrations.

Keep the definitions in sync with processes/support/migrations — the schema test fails on drift.
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

from .schemas import EMBED_DIM

METADATA = sa.MetaData()
_NOW = sa.text("now()")

TICKETS = sa.Table(
    "support_tickets",
    METADATA,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("external_id", sa.Text, nullable=False, unique=True),
    sa.Column("sender", sa.Text, nullable=False),
    sa.Column("subject", sa.Text, nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="new"),
    sa.Column("output", JSONB),
    sa.Column("decision", JSONB),
    sa.Column("reply", sa.Text),
    sa.Column("approved_by", sa.Text),
    sa.Column("mail", JSONB),
    sa.Column("outcome", sa.Text, sa.Computed("output->>'outcome'", persisted=True)),
    sa.Column("escalate_to", sa.Text, sa.Computed("output->>'escalate_to'", persisted=True)),
    sa.Column("version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
)
sa.Index(
    "support_tickets_message_id", TICKETS.c.mail["message_id"].astext, unique=True
)  # one row per email: a mail stored under an older key never comes in twice

KB = sa.Table(
    "support_kb",
    METADATA,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("embedding", Vector(EMBED_DIM), nullable=False),
)
