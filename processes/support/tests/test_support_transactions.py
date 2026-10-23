"""A replayed write matches no row: the status guards make the steps idempotent."""

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa

from mappd.db.migrate import sqlalchemy_url
from mappd.settings import Settings
from processes.support.db import TICKETS
from processes.support.schemas import SupportOutput
from processes.support.workflow import mark_sent, save_outcome

OUTPUT = SupportOutput(
    topic="tech",
    route="technical",
    urgency="low",
    language="en",
    outcome="reply",
    escalate_to=None,
    draft=None,
)


@pytest.fixture
def ticket(dbos_runtime: Settings) -> str:
    """A 'new' ticket in the session DB; the datasource points there via configure."""
    engine = sa.create_engine(sqlalchemy_url(dbos_runtime.database_url))
    try:
        TICKETS.create(engine, checkfirst=True)
    finally:
        engine.dispose()
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM support_tickets")
        conn.execute(
            "INSERT INTO support_tickets (external_id, sender, subject, body)"
            " VALUES ('tx-1', 's', 'sub', 'b')"
        )
    return "tx-1"


def _row(settings: Settings, external_id: str) -> dict[str, object]:
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            "SELECT status, version, output, mail FROM support_tickets WHERE external_id = %s",
            (external_id,),
        ).fetchone()
    assert row is not None
    output = row[2]
    assert output is None or isinstance(output, dict)
    return {"status": row[0], "version": row[1], "output": output, "mail": row[3]}


def test_save_outcome_twice_writes_once(dbos_runtime: Settings, ticket: str) -> None:
    """The second call — a replay outside a workflow — sees status != 'new' and stops."""
    dbos_async.run(save_outcome(ticket, "awaiting_approval", OUTPUT))
    dbos_async.run(save_outcome(ticket, "awaiting_approval", OUTPUT))
    row = _row(dbos_runtime, ticket)
    assert (row["status"], row["version"]) == ("awaiting_approval", 1)
    output = row["output"]
    assert isinstance(output, dict)
    assert output["route"] == "technical"


def test_mark_sent_twice_sends_once(dbos_runtime: Settings, ticket: str) -> None:
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE support_tickets SET status = 'approved' WHERE external_id = %s",
            (ticket,),
        )
    dbos_async.run(mark_sent(ticket, "<one@mappd.test>"))
    dbos_async.run(mark_sent(ticket, "<two@mappd.test>"))
    row = _row(dbos_runtime, ticket)
    assert (row["status"], row["version"]) == ("sent", 1)
    assert row["mail"] == {"sent_message_id": "<one@mappd.test>"}
