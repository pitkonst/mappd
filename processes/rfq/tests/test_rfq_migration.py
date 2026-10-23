from pathlib import Path

from mappd.db import connect, run
from mappd.db.lint import check, migration_files
from mappd.db.migrate import upgrade
from mappd.settings import Settings
from processes.rfq.db import RFQS

PROCESSES = Path(__file__).parents[2]


def test_rfq_migration_lints_and_applies(fresh_database_url: str) -> None:
    settings = Settings(database_url=fresh_database_url, processes_dir=PROCESSES)
    assert check(migration_files(settings)) == []
    upgrade(settings)
    with connect(settings) as conn:
        row = run(
            conn,
            RFQS.insert()
            .values(external_id="r-1", sender="s", subject="sub", body="b")
            .returning(RFQS.c.status, RFQS.c.version, RFQS.c.outcome, RFQS.c.decision),
        ).fetchone()
    assert row == {"status": "new", "version": 0, "outcome": None, "decision": None}
