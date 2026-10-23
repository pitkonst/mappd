"""Table metadata of the installation process; the source of truth is the migrations."""

from typing import Any

import sqlalchemy as sa
from psycopg.types.json import Jsonb
from sqlalchemy.dialects.postgresql import JSONB, UUID

from mappd import db

METADATA = sa.MetaData()
_NOW = sa.text("now()")
_TS = sa.DateTime(timezone=True)

INSTALLERS = sa.Table(
    "installation_installers",
    METADATA,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("name", sa.Text, nullable=False, unique=True),
    sa.Column("link_token", sa.Text, unique=True),
    sa.Column("chat_id", sa.BigInteger, unique=True),
    sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
)

JOBS = sa.Table(
    "installation_jobs",
    METADATA,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("external_id", sa.Text, nullable=False, unique=True),
    sa.Column(
        "installer_id", sa.BigInteger, sa.ForeignKey("installation_installers.id"), nullable=False
    ),
    sa.Column("site", sa.Text, nullable=False),
    sa.Column("skus", JSONB, nullable=False),
    sa.Column("appointment_at", _TS, nullable=False),
    sa.Column("state", sa.Text, nullable=False, server_default="assigned"),
    sa.Column("blocked_from", sa.Text),
    sa.Column("blocked_reason", sa.Text),
    sa.Column("version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
    sa.Column("updated_at", _TS, nullable=False, server_default=_NOW),
    # The dispatcher holding the conversation; while set, the agent neither replies nor moves.
    sa.Column("taken_over_by", sa.Text),
)

MESSAGES = sa.Table(
    "installation_messages",
    METADATA,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("job_id", sa.BigInteger, sa.ForeignKey("installation_jobs.id")),
    sa.Column("chat_id", sa.BigInteger, nullable=False),
    sa.Column("direction", sa.Text, nullable=False),
    sa.Column("channel_key", sa.Text, unique=True),
    sa.Column("text", sa.Text),
    sa.Column("photo_ref", sa.Text),
    sa.Column("photo_description", sa.Text),
    sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
    # installer | bot | dispatcher:<name>
    sa.Column("author", sa.Text, nullable=False, server_default="installer"),
    # the inbound message this one answers: the timeline shows it right after that message
    sa.Column("reply_to", sa.BigInteger, sa.ForeignKey("installation_messages.id")),
)

DEADLINES = sa.Table(
    "installation_deadlines",
    METADATA,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("job_id", sa.BigInteger, sa.ForeignKey("installation_jobs.id"), nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("due_at", _TS, nullable=False),
    sa.Column("settled_at", _TS),
    sa.Column("notified_at", _TS),
    sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
)

POLLING = sa.Table(
    "installation_polling",
    METADATA,
    sa.Column("bot", sa.Text, primary_key=True),
    sa.Column("offset", sa.BigInteger, nullable=False, server_default="0"),
)

EVENTS = sa.Table(
    "installation_events",
    METADATA,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("job_id", sa.BigInteger, sa.ForeignKey("installation_jobs.id"), nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    # installer | bot | agent | dispatcher:<name> | system
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
    # the inbound message whose turn recorded it (a transition, a tool call), if any
    sa.Column("message_id", sa.BigInteger, sa.ForeignKey("installation_messages.id")),
)

PROPOSALS = sa.Table(
    "installation_proposals",
    METADATA,
    sa.Column(
        "id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")
    ),
    sa.Column("job_id", sa.BigInteger, sa.ForeignKey("installation_jobs.id"), nullable=False),
    sa.Column("tool", sa.Text, nullable=False),
    sa.Column("arguments", JSONB, nullable=False),
    sa.Column("reason", sa.Text, nullable=False, server_default=""),
    # pending -> confirmed -> done | failed; pending -> rejected
    sa.Column("status", sa.Text, nullable=False, server_default="pending"),
    sa.Column("decided_by", sa.Text),
    sa.Column("result", JSONB),
    sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
    sa.Column("decided_at", _TS),
)


def record_event(
    conn: db.Connection,
    job_id: int,
    kind: str,
    actor: str,
    data: dict[str, Any] | None = None,
    *,
    message_id: int | None = None,
) -> None:
    """One timeline event, inside the caller's transaction (the change it records).

    `message_id` is the inbound message whose turn this is, so the event shows with it.
    """
    db.run(
        conn,
        sa.insert(EVENTS).values(
            job_id=job_id, kind=kind, actor=actor, data=Jsonb(data or {}), message_id=message_id
        ),
    )


# A job a dispatcher should look at beyond being blocked (/ops counts it with the blocked ones,
# and the jobs page's "needs you" tab shows the same): a proposal to decide, or a deadline
# passed. A handed-over job never is.
NEEDS_DISPATCHER: sa.ColumnElement[bool] = sa.and_(
    JOBS.c.state != "handed_over",
    sa.or_(
        sa.exists().where(PROPOSALS.c.job_id == JOBS.c.id, PROPOSALS.c.status == "pending"),
        sa.exists().where(
            DEADLINES.c.job_id == JOBS.c.id,
            DEADLINES.c.settled_at.is_(None),
            DEADLINES.c.due_at <= sa.func.now(),
        ),
    ),
)
