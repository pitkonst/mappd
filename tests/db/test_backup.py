import json
import os
import sys
from typing import TYPE_CHECKING

import psycopg
import pytest

from mappd.db.backup import MANIFEST, BackupError, backup, restore
from mappd.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture
def databases(recreate_database: Callable[[str], str]) -> Settings:
    settings = Settings(
        database_url=recreate_database("mappd_test_backup"),
        system_database_url=recreate_database("mappd_test_backup_dbos"),
        app_version="v1",
    )
    for url in (settings.database_url, settings.system_database_url):
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("CREATE TABLE marks (note text)")
            conn.execute("INSERT INTO marks VALUES ('before')")
    return settings


def notes(url: str) -> list[str]:
    with psycopg.connect(url) as conn:
        return [r[0] for r in conn.execute("SELECT note FROM marks ORDER BY note")]


def test_backup_then_restore_brings_both_databases_back(
    databases: Settings, pg_tools: Path, tmp_path: Path
) -> None:
    target = tmp_path / "backup-1"
    assert backup(databases, target) == target
    assert sorted(p.name for p in target.iterdir()) == ["app.dump", MANIFEST, "system.dump"]
    manifest = json.loads((target / MANIFEST).read_text())
    assert manifest["format"] == 1
    assert manifest["app_version"] == "v1"
    for url in (databases.database_url, databases.system_database_url):
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("INSERT INTO marks VALUES ('after')")
    restore(databases, target)
    assert notes(databases.database_url) == ["before"]
    assert notes(databases.system_database_url) == ["before"]


def test_backup_refuses_a_directory_with_files(tmp_path: Path) -> None:
    (tmp_path / "old.dump").write_text("keep me")
    with pytest.raises(BackupError, match="not empty"):
        backup(Settings(), tmp_path)
    assert (tmp_path / "old.dump").read_text() == "keep me"


def test_restore_needs_a_manifest(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match=MANIFEST):
        restore(Settings(), tmp_path)


def test_missing_tool_is_a_backup_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(BackupError, match="pg_dump not found"):
        backup(Settings(), tmp_path / "b")


def test_failed_dump_is_a_backup_error(
    recreate_database: Callable[[str], str], pg_tools: Path, tmp_path: Path
) -> None:
    url = recreate_database("mappd_test_backup_missing").rsplit("/", 1)[0] + "/no_such_db"
    with pytest.raises(BackupError, match="pg_dump failed"):
        backup(Settings(database_url=url, system_database_url=url), tmp_path / "b")


@pytest.fixture
def recording_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """pg_dump stand-in that logs its argv and PGPASSWORD instead of dumping."""
    log = tmp_path / "calls.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "pg_dump"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as f:\n"
        "    call = {'argv': sys.argv[1:], 'password': os.environ.get('PGPASSWORD')}\n"
        "    f.write(json.dumps(call))\n"
        "    f.write('\\n')\n"
        "sys.stdout.write('dump')\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def test_backup_dumps_the_system_database_first_and_keeps_passwords_out_of_argv(
    recording_tools: Path, tmp_path: Path
) -> None:
    settings = Settings(
        database_url="postgresql://mappd:app-s3cret@db:5432/mappd",
        system_database_url="postgresql://mappd:sys-s3cret@db:5432/mappd_dbos",
    )
    backup(settings, tmp_path / "b")
    calls = [json.loads(line) for line in recording_tools.read_text().splitlines()]
    assert [c["password"] for c in calls] == ["sys-s3cret", "app-s3cret"]
    assert "dbname=mappd_dbos" in " ".join(calls[0]["argv"])
    assert all("s3cret" not in arg for c in calls for arg in c["argv"])


def test_restore_removes_objects_created_after_the_backup(
    databases: Settings, pg_tools: Path, tmp_path: Path
) -> None:
    target = tmp_path / "b"
    backup(databases, target)
    for url in (databases.database_url, databases.system_database_url):
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("CREATE TABLE later (x int)")
    restore(databases, target)
    for url in (databases.database_url, databases.system_database_url):
        with psycopg.connect(url) as conn:
            row = conn.execute("SELECT to_regclass('later') IS NULL").fetchone()
        assert row == (True,)
        assert notes(url) == ["before"]


def test_a_broken_dump_is_found_before_anything_is_replaced(
    databases: Settings, pg_tools: Path, tmp_path: Path
) -> None:
    target = tmp_path / "b"
    backup(databases, target)
    (target / "system.dump").write_bytes(b"not a dump")
    with psycopg.connect(databases.database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO marks VALUES ('after')")
    with pytest.raises(BackupError, match=r"system\.dump"):
        restore(databases, target)
    assert notes(databases.database_url) == ["after", "before"], "the app database is untouched"
