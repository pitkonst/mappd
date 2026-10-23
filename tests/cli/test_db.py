import json
import os
from typing import TYPE_CHECKING

import psycopg
import pytest
from typer.testing import CliRunner

from mappd.cli import app
from mappd.db.backup import MANIFEST

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

runner = CliRunner()

DROP_MIGRATION = """
revision = "bad_0001"
down_revision = None
branch_labels = ("bad",)
depends_on = "mappd_0001"


def upgrade():
    op.drop_table("actions")
"""


@pytest.fixture
def env(
    fresh_database_url: str,
    recreate_database: Callable[[str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    processes = tmp_path / "processes"
    processes.mkdir()
    monkeypatch.setenv("MAPPD_DATABASE_URL", fresh_database_url)
    monkeypatch.setenv("MAPPD_SYSTEM_DATABASE_URL", recreate_database("mappd_test_cli_dbos"))
    monkeypatch.setenv("MAPPD_PROCESSES_DIR", str(processes))
    return processes


def test_migrate_applies_then_reports_up_to_date(env: Path) -> None:
    first = runner.invoke(app, ["db", "migrate", "--json"])
    assert first.exit_code == 0
    assert json.loads(first.stdout) == {
        "applied": ["mappd_0001", "mappd_0002", "mappd_0003", "mappd_0004", "mappd_0005"]
    }
    second = runner.invoke(app, ["db", "migrate"])
    assert second.exit_code == 0
    assert second.stdout.strip() == "up to date"
    with psycopg.connect(os.environ["MAPPD_SYSTEM_DATABASE_URL"]) as conn:
        found = conn.execute("SELECT to_regclass('dbos.workflow_status')").fetchone()
    assert found is not None
    assert found[0] is not None, "DBOS's system schema is in place before the app starts"


def test_migrate_with_database_down_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPPD_DATABASE_URL", "postgresql://u:p@127.0.0.1:9/db")
    r = runner.invoke(app, ["db", "migrate"])
    assert r.exit_code == 1
    assert r.stderr.startswith("error: ")


def test_migrate_on_a_database_ahead_does_nothing(env: Path) -> None:
    # A rollback: the database holds a revision this code does not know (expand-only rule).
    runner.invoke(app, ["db", "migrate"])
    with psycopg.connect(os.environ["MAPPD_DATABASE_URL"], autocommit=True) as conn:
        conn.execute("INSERT INTO alembic_version (version_num) VALUES ('feedfacecafe')")
    r = runner.invoke(app, ["db", "migrate"])
    assert r.exit_code == 0
    assert "database is ahead of this code (feedfacecafe): nothing to migrate" in r.stdout
    r = runner.invoke(app, ["db", "migrate", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.stdout) == {"applied": [], "ahead": ["feedfacecafe"]}


def test_lint_passes_on_the_core(env: Path) -> None:
    r = runner.invoke(app, ["db", "lint", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.stdout) == {"ok": True, "violations": []}


def test_lint_fails_on_a_drop(env: Path) -> None:
    (env / "bad" / "migrations").mkdir(parents=True)
    (env / "bad" / "migrations" / "bad_0001.py").write_text(DROP_MIGRATION)
    r = runner.invoke(app, ["db", "lint", "--json"])
    assert r.exit_code == 1
    [violation] = json.loads(r.stdout)["violations"]
    assert violation["path"].endswith("bad_0001.py")
    assert "op.drop_table" in violation["message"]


def test_restore_without_force_refuses(tmp_path: Path) -> None:
    r = runner.invoke(app, ["db", "restore", str(tmp_path)])
    assert r.exit_code == 2
    assert "--force" in r.stderr


def test_backup_and_restore_round_trip(
    recreate_database: Callable[[str], str],
    pg_tools: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAPPD_DATABASE_URL", recreate_database("mappd_test_cli_backup"))
    monkeypatch.setenv("MAPPD_SYSTEM_DATABASE_URL", recreate_database("mappd_test_cli_backup_dbos"))
    target = tmp_path / "b"
    r = runner.invoke(app, ["db", "backup", str(target), "--json"])
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout) == {"directory": str(target), "status": "backed_up"}
    assert (target / MANIFEST).is_file()
    r = runner.invoke(app, ["db", "restore", str(target), "--force"])
    assert r.exit_code == 0, r.stderr
