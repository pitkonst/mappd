"""Schema migrations with Alembic, configured in code.

Core migrations live in mappd/db/versions on the branch `mappd`; each process keeps its own in
processes/<name>/migrations on a branch named after it. `upgrade` applies all heads. Migrations
are expand-only within a deploy (see mappd.db.lint).
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool

if TYPE_CHECKING:
    from mappd.settings import Settings

DB_DIR = Path(__file__).parent
SCRIPT_LOCATION = DB_DIR / "alembic_env"
CORE_VERSIONS = DB_DIR / "versions"


def sqlalchemy_url(url: str) -> str:
    """Point a libpq-style URL at SQLAlchemy's psycopg 3 driver."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url.removeprefix(prefix)
    return url


def version_locations(settings: Settings) -> list[Path]:
    """Core versions first, then every processes/<name>/migrations directory, by name."""
    processes = settings.processes_dir
    found = sorted(processes.glob("*/migrations")) if processes.is_dir() else []
    return [CORE_VERSIONS, *(p for p in found if p.is_dir())]


def alembic_config(settings: Settings) -> Config:
    """Alembic config for this project; no alembic.ini needed."""
    cfg = Config()
    locations = os.pathsep.join(str(p) for p in version_locations(settings))
    options = {
        "script_location": str(SCRIPT_LOCATION),
        "path_separator": "os",
        "version_locations": locations,
        "sqlalchemy.url": sqlalchemy_url(settings.database_url),
    }
    for name, value in options.items():
        # ConfigParser interpolates `%`: escape it so passwords and paths with `%` survive.
        cfg.set_main_option(name, value.replace("%", "%%"))
    return cfg


def _heads(settings: Settings) -> tuple[str, ...]:
    """The revisions the app database currently reports."""
    engine = create_engine(sqlalchemy_url(settings.database_url), poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_heads()
    finally:
        engine.dispose()


def _known(cfg: Config) -> set[str]:
    """Every revision id this code's migration scripts define."""
    return {s.revision for s in ScriptDirectory.from_config(cfg).walk_revisions()}


def ahead(settings: Settings) -> list[str]:
    """Revisions in the app database that this code does not know.

    Non-empty after a rollback: the newer code migrated the database further. Migrations are
    expand-only (mappd.db.lint), so the running code's schema is a subset and nothing is done.
    """
    known = _known(alembic_config(settings))
    return [r for r in _heads(settings) if r not in known]


def upgrade(settings: Settings) -> list[str]:
    """Apply every pending migration of the core and all processes, then the datasource's.

    Returns:
        The revisions the database is ahead by ([] normally). When it is non-empty — a
        rollback — nothing is applied: the known schema is a subset of the database's.
    """
    # Deferred: mappd.db.transaction imports sqlalchemy_url from this module.
    from mappd.db.transaction import migrate as migrate_datasource  # noqa: PLC0415

    cfg = alembic_config(settings)
    unknown = [r for r in _heads(settings) if r not in _known(cfg)]
    if unknown:
        return unknown
    command.upgrade(cfg, "heads")
    migrate_datasource(settings)
    return []


def system(settings: Settings) -> None:
    """Create or migrate DBOS's system schema, as the app would on its first launch.

    DBOS's own migrations are forward-compatible, so they run on a rollback too.
    """
    from dbos import DBOS  # noqa: PLC0415 - only this command needs DBOS here

    DBOS.migrate(settings.system_database_url)


def pending(settings: Settings) -> list[str]:
    """Revisions not yet applied to the app database, dependencies first.

    Empty when the database is ahead of this code (a rollback): nothing is pending then.
    """
    cfg = alembic_config(settings)
    current = _heads(settings)
    if any(r not in _known(cfg) for r in current):
        return []
    script = ScriptDirectory.from_config(cfg)
    applied = (
        {r.revision for r in script.revision_map.iterate_revisions(current, "base")}
        if current
        else set()
    )
    ordered = [s.revision for s in reversed(list(script.walk_revisions()))]
    return [r for r in ordered if r not in applied]
