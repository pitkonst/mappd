from pathlib import Path

import psycopg

from mappd.db import connect, run
from mappd.db.lint import check, migration_files
from mappd.db.migrate import upgrade
from mappd.settings import Settings
from processes.support.db import KB, TICKETS

PROCESSES = Path(__file__).parents[2]


def test_support_migration_lints_and_applies(fresh_database_url: str) -> None:
    settings = Settings(database_url=fresh_database_url, processes_dir=PROCESSES)
    assert check(migration_files(settings)) == []
    upgrade(settings)
    with connect(settings) as conn:
        run(
            conn,
            KB.insert().values(id="kb-1", title="t", body="b", embedding=str([0.0] * 64)),
        )
        row = run(
            conn,
            TICKETS.insert()
            .values(external_id="m-1", sender="s", subject="sub", body="b")
            .returning(
                TICKETS.c.status, TICKETS.c.version, TICKETS.c.outcome, TICKETS.c.escalate_to
            ),
        ).fetchone()
    assert row == {"status": "new", "version": 0, "outcome": None, "escalate_to": None}


def test_generated_columns_read_output(fresh_database_url: str) -> None:
    """outcome and escalate_to are generated from the `output` JSONB."""
    settings = Settings(database_url=fresh_database_url, processes_dir=PROCESSES)
    upgrade(settings)
    with psycopg.connect(fresh_database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO support_tickets"
            " (external_id, sender, subject, body, status, output, version)"
            " VALUES ('old-1', 's', 'sub', 'b', 'awaiting_approval',"
            '         \'{"topic":"tech","route":"technical","urgency":"low",'
            '          "language":"en","outcome":"reply",'
            '          "draft":{"reply":"hi","citations":["kb-1"]}}\'::jsonb,'
            "         3)"
        )
    with psycopg.connect(fresh_database_url) as conn:
        row = conn.execute(
            "SELECT outcome, escalate_to, version FROM support_tickets WHERE external_id = 'old-1'"
        ).fetchone()
    assert row is not None
    outcome, escalate_to, version = row
    assert (outcome, escalate_to) == ("reply", None), "generated columns read output"
    assert version == 3


def test_0002_adds_the_decision_column(fresh_database_url: str) -> None:
    settings = Settings(database_url=fresh_database_url, processes_dir=PROCESSES)
    upgrade(settings)
    with psycopg.connect(fresh_database_url) as conn:
        row = conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns"
            " WHERE table_name = 'support_tickets' AND column_name = 'decision'"
        ).fetchone()
    assert row == ("jsonb", "YES")
