"""Backup and restore of the app and DBOS databases, always together.

Workflow IDs live in both: the app's rows name their workflows, and the DBOS journal replays by
workflow ID. Restoring one without the other loses or replays work.

Uses pg_dump and pg_restore from PATH, streaming through stdout and stdin.
"""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

if TYPE_CHECKING:
    from mappd.settings import Settings

FORMAT = 1
MANIFEST = "manifest.json"


class BackupError(RuntimeError):
    """A backup or restore could not be done."""


def _databases(settings: Settings) -> dict[str, str]:
    # Dump order matters for a running app: the system database first. A step it records as
    # done then has its rows in the later app dump; the reverse could journal a step whose rows
    # are missing, and DBOS would never redo it. A step missing from the journal just re-runs,
    # which idempotency keys absorb (AGENTS.md, rule 2).
    return {"system": settings.system_database_url, "app": settings.database_url}


def _connection(url: str) -> tuple[str, dict[str, str]]:
    """Libpq conninfo without the password, and an environment carrying it.

    Keeps the password out of the tools' argv, which other users can read in the process list.
    """
    params = {k: str(v) for k, v in conninfo_to_dict(url).items()}
    password = params.pop("password", None)
    env = dict(os.environ)
    if password is not None:
        env["PGPASSWORD"] = password
    return make_conninfo(**params), env


def _empty(url: str) -> None:
    """Drop every schema, then recreate public: a restore must not keep newer tables."""
    with psycopg.connect(url, autocommit=True) as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT nspname FROM pg_namespace"
                " WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'"
            )
        ]
        for name in names:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
        conn.execute("CREATE SCHEMA public")


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BackupError(f"{name} not found on PATH; install the PostgreSQL client tools")
    return path


def _run(
    args: list[str],
    *,
    stdin: IO[bytes] | None = None,
    stdout: IO[bytes] | None = None,
    env: dict[str, str] | None = None,
) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        args, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, env=env, check=False
    )
    if result.returncode != 0:
        tool = Path(args[0]).name
        raise BackupError(f"{tool} failed: {result.stderr.decode(errors='replace').strip()}")


def backup(settings: Settings, directory: Path) -> Path:
    """Dump both databases into `directory`, which must be new or empty."""
    if directory.exists() and any(directory.iterdir()):
        raise BackupError(f"{directory} is not empty; choose a new directory")
    pg_dump = _tool("pg_dump")
    directory.mkdir(parents=True, exist_ok=True)
    for name, url in _databases(settings).items():
        dsn, env = _connection(url)
        with (directory / f"{name}.dump").open("wb") as out:
            _run([pg_dump, "--format=custom", f"--dbname={dsn}"], stdout=out, env=env)
    manifest = {
        "format": FORMAT,
        "app_version": settings.app_version,
        "created_at": datetime.now(UTC).isoformat(),
        "databases": {name: f"{name}.dump" for name in _databases(settings)},
    }
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    return directory


def restore(settings: Settings, directory: Path) -> None:
    """Replace both databases with the backup in `directory`. Stop the app first.

    Both dumps are checked before either database is touched; each database is emptied, so
    tables created after the backup do not survive it.
    """
    manifest_path = directory / MANIFEST
    if not manifest_path.is_file():
        raise BackupError(f"{manifest_path} not found; is {directory} a mappd backup?")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("format") != FORMAT:
        raise BackupError(f"unsupported backup format {manifest.get('format')!r}")
    dumps = {name: directory / file for name, file in manifest["databases"].items()}
    if set(dumps) != set(_databases(settings)) or not all(p.is_file() for p in dumps.values()):
        raise BackupError(f"{directory} must hold app.dump and system.dump")
    pg_restore = _tool("pg_restore")
    for path in dumps.values():
        with path.open("rb") as dump:
            try:
                _run([pg_restore, "--list"], stdin=dump, stdout=subprocess.DEVNULL)  # type: ignore[arg-type]
            except BackupError as e:
                raise BackupError(f"{path.name} is not a usable dump: {e}") from e
    replaced: list[str] = []
    for name, url in _databases(settings).items():
        dsn, env = _connection(url)
        try:
            _empty(url)
            with dumps[name].open("rb") as dump:
                _run(
                    [pg_restore, "--no-owner", "--single-transaction", f"--dbname={dsn}"],
                    stdin=dump,
                    env=env,
                )
        except (BackupError, psycopg.Error) as e:
            note = (
                f"; the {replaced[0]} database was already replaced, re-run restore"
                if replaced
                else ""
            )
            raise BackupError(f"restoring the {name} database failed{note}: {e}") from e
        replaced.append(name)
