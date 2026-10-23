from pathlib import Path

from mappd.db import connect
from mappd.db.migrate import (
    ahead,
    alembic_config,
    pending,
    sqlalchemy_url,
    upgrade,
    version_locations,
)
from mappd.settings import Settings

CORE = Path(__file__).parents[2] / "src" / "mappd" / "db" / "versions"

DEMO_MIGRATION = """
from collections.abc import Sequence

from alembic import op

revision: str = "demo_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = ("demo",)
depends_on: str | Sequence[str] | None = "mappd_0001"


def upgrade() -> None:
    op.execute("CREATE TABLE demo_items (id bigserial PRIMARY KEY, name text NOT NULL)")


def downgrade() -> None:
    op.drop_table("demo_items")
"""


def test_sqlalchemy_url_selects_psycopg3() -> None:
    assert sqlalchemy_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert sqlalchemy_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert sqlalchemy_url("postgresql+psycopg://h/db") == "postgresql+psycopg://h/db"


def test_version_locations_are_core_then_processes_with_migrations(tmp_path: Path) -> None:
    (tmp_path / "beta" / "migrations").mkdir(parents=True)
    (tmp_path / "alpha" / "migrations").mkdir(parents=True)
    (tmp_path / "no_migrations").mkdir()
    locations = version_locations(Settings(processes_dir=tmp_path))
    assert locations == [CORE, tmp_path / "alpha" / "migrations", tmp_path / "beta" / "migrations"]


def test_missing_processes_dir_means_core_only(tmp_path: Path) -> None:
    assert version_locations(Settings(processes_dir=tmp_path / "absent")) == [CORE]


def test_percent_in_password_and_paths_survives_config_interpolation(tmp_path: Path) -> None:
    processes = tmp_path / "100%"
    (processes / "demo" / "migrations").mkdir(parents=True)
    cfg = alembic_config(
        Settings(database_url="postgresql://u:p%40ss@h/db", processes_dir=processes)
    )
    assert cfg.get_main_option("sqlalchemy.url") == "postgresql+psycopg://u:p%40ss@h/db"
    assert str(processes / "demo" / "migrations") in (
        cfg.get_main_option("version_locations") or ""
    )


def test_upgrade_creates_the_actions_table_and_is_idempotent(
    fresh_database_url: str, tmp_path: Path
) -> None:
    settings = Settings(database_url=fresh_database_url, processes_dir=tmp_path)
    upgrade(settings)
    upgrade(settings)
    with connect(settings) as conn:
        row = conn.execute("SELECT to_regclass('actions') IS NOT NULL AS exists").fetchone()
        ds = conn.execute(
            "SELECT to_regclass('dbos.datasource_outputs') IS NOT NULL AS exists"
        ).fetchone()
    assert row == {"exists": True}
    assert ds == {"exists": True}, "upgrade also migrates the datasource tables"


def test_upgrade_applies_process_migrations(fresh_database_url: str, tmp_path: Path) -> None:
    migrations = tmp_path / "demo" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "demo_0001_items.py").write_text(DEMO_MIGRATION)
    settings = Settings(database_url=fresh_database_url, processes_dir=tmp_path)
    upgrade(settings)
    with connect(settings) as conn:
        conn.execute("INSERT INTO demo_items (name) VALUES ('ok')")
        actions = conn.execute("SELECT to_regclass('actions') IS NOT NULL AS exists").fetchone()
        heads = {r["version_num"] for r in conn.execute("SELECT version_num FROM alembic_version")}
    assert actions == {"exists": True}, "the core migration the process depends on ran too"
    # depends_on makes mappd_0001 an ancestor of demo_0001: one head per branch tip.
    assert heads == {"demo_0001", "mappd_0005"}


def test_pending_lists_unapplied_revisions_dependencies_first(
    fresh_database_url: str, tmp_path: Path
) -> None:
    migrations = tmp_path / "demo" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "demo_0001_items.py").write_text(DEMO_MIGRATION)
    settings = Settings(database_url=fresh_database_url, processes_dir=tmp_path)
    assert pending(settings) == [
        "mappd_0001",
        "demo_0001",
        "mappd_0002",
        "mappd_0003",
        "mappd_0004",
        "mappd_0005",
    ]
    upgrade(settings)
    assert pending(settings) == []


def test_a_database_ahead_of_this_code_is_left_alone(
    fresh_database_url: str, tmp_path: Path
) -> None:
    # A rollback: the newer code migrated further, leaving a revision this code cannot know.
    settings = Settings(database_url=fresh_database_url, processes_dir=tmp_path)
    assert upgrade(settings) == []
    with connect(settings) as conn:
        conn.execute("INSERT INTO alembic_version (version_num) VALUES ('feedfacecafe')")
    assert ahead(settings) == ["feedfacecafe"]
    assert pending(settings) == []
    assert upgrade(settings) == ["feedfacecafe"]
