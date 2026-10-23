"""mappd db: the P layer, Postgres. Migrations, the migration linter, backup and restore."""

from pathlib import Path
from typing import Annotated

import typer

from mappd.cli._common import (
    DB_ERRORS,
    EXIT_FAILED,
    EXIT_USAGE,
    JsonFlag,
    emit,
    fail,
    load_settings,
    status,
)
from mappd.db import backup as backup_module
from mappd.db import lint as lint_module
from mappd.db import migrate as migrate_module

app = typer.Typer(help="P: the Postgres databases.", no_args_is_help=True)


@app.command()
def migrate(as_json: JsonFlag = False) -> None:
    """Apply every pending migration of the core and the processes, and DBOS's system schema.

    The compose `migrate` service runs this before the app starts.

    Example:
        mappd db migrate --json
    """
    settings = load_settings()
    try:
        todo = migrate_module.pending(settings)
        ahead = migrate_module.upgrade(settings)
        migrate_module.system(settings)
    except DB_ERRORS as e:
        fail(f"database unavailable: {e}")
    if ahead:
        emit(
            {"applied": todo, "ahead": ahead},
            as_json,
            [
                f"database is ahead of this code ({', '.join(ahead)}): nothing to migrate — "
                "expected after a rollback; migrations are expand-only"
            ],
        )
        return
    emit({"applied": todo}, as_json, [f"applied {r}" for r in todo] or ["up to date"])


@app.command()
def lint(as_json: JsonFlag = False) -> None:
    """Check that migrations only expand the schema; drops and renames need `contract = True`.

    Exit 1 when a migration violates the rule.

    Example:
        mappd db lint
    """
    settings = load_settings()
    violations = lint_module.check(lint_module.migration_files(settings))
    emit(
        {
            "ok": not violations,
            "violations": [
                {"path": str(v.path), "line": v.line, "message": v.message} for v in violations
            ],
        },
        as_json,
        [str(v) for v in violations] or ["ok: every migration only expands"],
    )
    if violations:
        raise typer.Exit(EXIT_FAILED)


@app.command()
def backup(
    directory: Annotated[Path, typer.Argument(help="A new or empty directory for the backup.")],
    as_json: JsonFlag = False,
) -> None:
    """Back up the app and DBOS databases together into DIRECTORY (needs pg_dump).

    Example:
        mappd db backup backups/2026-10-12 --json
    """
    settings = load_settings()
    try:
        with status("backing up…", as_json):
            backup_module.backup(settings, directory)
    except backup_module.BackupError as e:
        fail(str(e))
    emit(
        {"directory": str(directory), "status": "backed_up"},
        as_json,
        [f"backed up to {directory}"],
    )


@app.command()
def restore(
    directory: Annotated[Path, typer.Argument(help="A directory written by `mappd db backup`.")],
    force: Annotated[
        bool, typer.Option("--force", help="Required: restore replaces both databases.")
    ] = False,
    as_json: JsonFlag = False,
) -> None:
    """Replace the app and DBOS databases with a backup (needs pg_restore). Stop the app first.

    Example:
        mappd db restore backups/2026-10-12 --force
    """
    if not force:
        fail(
            "restore replaces the app and DBOS databases; stop the app and pass --force", EXIT_USAGE
        )
    settings = load_settings()
    try:
        with status("restoring…", as_json):
            backup_module.restore(settings, directory)
    except backup_module.BackupError as e:
        fail(str(e))
    emit(
        {"directory": str(directory), "status": "restored"},
        as_json,
        [f"restored from {directory}"],
    )
