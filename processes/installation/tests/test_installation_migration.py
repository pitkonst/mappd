from pathlib import Path

from psycopg.types.json import Jsonb

from mappd.db import connect, run
from mappd.db.lint import check, migration_files
from mappd.db.migrate import upgrade
from mappd.settings import Settings
from processes.installation.db import INSTALLERS, JOBS

PROCESSES = Path(__file__).parents[2]


def test_installation_migration_lints_and_applies(fresh_database_url: str) -> None:
    settings = Settings(database_url=fresh_database_url, processes_dir=PROCESSES)
    assert check(migration_files(settings)) == []
    upgrade(settings)
    with connect(settings) as conn:
        installer = run(
            conn, INSTALLERS.insert().values(name="Ivan").returning(INSTALLERS.c.id)
        ).fetchone()
        assert installer is not None
        row = run(
            conn,
            JOBS.insert()
            .values(
                external_id="j-1",
                installer_id=installer["id"],
                site="Riverside",
                skus=Jsonb(["CTL-H400"]),
                appointment_at="2026-10-16T09:00:00Z",
            )
            .returning(JOBS.c.state, JOBS.c.version, JOBS.c.blocked_from),
        ).fetchone()
    assert row == {"state": "assigned", "version": 0, "blocked_from": None}


def test_the_timeline_migration_backfills_authors(fresh_database_url: str) -> None:
    from alembic import command  # noqa: PLC0415

    from mappd.db.migrate import alembic_config  # noqa: PLC0415
    from processes.installation.db import EVENTS, MESSAGES, PROPOSALS  # noqa: PLC0415

    settings = Settings(database_url=fresh_database_url, processes_dir=PROCESSES)
    command.upgrade(alembic_config(settings), "installation_0002")
    with connect(settings) as conn:
        installer = conn.execute(
            "INSERT INTO installation_installers (name) VALUES ('Ivan') RETURNING id"
        ).fetchone()
        assert installer is not None
        job = conn.execute(
            "INSERT INTO installation_jobs (external_id, installer_id, site, skus, appointment_at)"
            " VALUES ('j-1', %s, 'Riverside', '[]', now()) RETURNING id",
            (installer["id"],),
        ).fetchone()
        assert job is not None
        conn.execute(
            "INSERT INTO installation_messages (job_id, chat_id, direction, text) VALUES"
            " (%s, 1, 'in', 'hi'), (%s, 1, 'out', 'hello')",
            (job["id"], job["id"]),
        )
    upgrade(settings)
    with connect(settings) as conn:
        authors = [
            r["author"] for r in run(conn, MESSAGES.select().order_by(MESSAGES.c.id)).fetchall()
        ]
        run(
            conn, EVENTS.insert().values(job_id=job["id"], kind="takeover", actor="dispatcher:olga")
        )
        proposal = run(
            conn,
            PROPOSALS.insert()
            .values(job_id=job["id"], tool="reserve_parts", arguments=Jsonb({"skus": ["CTL-H400"]}))
            .returning(PROPOSALS.c.status, PROPOSALS.c.id),
        ).fetchone()
        taken = conn.execute("SELECT taken_over_by FROM installation_jobs").fetchone()
        assert taken is not None
    assert authors == ["installer", "bot"]
    assert proposal is not None
    assert proposal["status"] == "pending"
    assert taken["taken_over_by"] is None
