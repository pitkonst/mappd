"""Table metadata of the RFQ process; the source of truth is the migrations.

Keep the definitions in sync with processes/rfq/migrations.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

METADATA = sa.MetaData()
_NOW = sa.text("now()")

RFQS = sa.Table(
    "rfq_requests",
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
    "rfq_requests_message_id", RFQS.c.mail["message_id"].astext, unique=True
)  # one row per email: a mail stored under an older key never comes in twice
