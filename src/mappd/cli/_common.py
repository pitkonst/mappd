"""Shared pieces of the CLI: settings, output, exit codes."""

import contextlib
import json
import logging
import sys
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import TYPE_CHECKING, Annotated, NoReturn

import psycopg
import sqlalchemy.exc
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)

from mappd.settings import Settings, SettingsError, environment

if TYPE_CHECKING:
    from collections.abc import Iterable

EXIT_FAILED = 1
EXIT_USAGE = 2


JSON_MODE: ContextVar[bool] = ContextVar("mappd_json", default=False)


def _record_json(value: bool) -> bool:
    """Remember --json for `fail`, which exits before the command returns."""
    JSON_MODE.set(value)
    return value


JsonFlag = Annotated[
    bool,
    typer.Option("--json", callback=_record_json, help="Print machine-readable JSON on stdout."),
]

DB_ERRORS = (sqlalchemy.exc.OperationalError, psycopg.OperationalError)

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def configure_logging() -> None:
    """The CLI owns logging: libraries' `basicConfig` calls afterwards are no-ops."""
    value = environment().get("MAPPD_LOG_LEVEL", "WARNING")
    level = value.upper()
    if level not in LOG_LEVELS:
        fail(
            f"MAPPD_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, not {value!r}",
            EXIT_USAGE,
        )
    logging.basicConfig(
        level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )


def fail(message: str, code: int = EXIT_FAILED) -> NoReturn:
    """Report `message` and exit with `code`; a JSON object when --json was passed."""
    if JSON_MODE.get():
        typer.echo(json.dumps({"ok": False, "error": message, "exit_code": code}))
    else:
        typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def load_settings() -> Settings:
    """Settings from the environment; invalid settings exit 2."""
    try:
        return Settings.from_env()
    except SettingsError as e:
        fail(str(e), EXIT_USAGE)


def emit(data: object, as_json: bool, lines: Iterable[str]) -> None:
    """Print `data` as one line of JSON, or the human-readable `lines`."""
    if as_json:
        typer.echo(json.dumps(data, default=str))
        return
    for line in lines:
        typer.echo(line)


def progress(
    label: str, as_json: bool
) -> contextlib.AbstractContextManager[Callable[[int, int], None] | None]:
    """A progress bar on stderr for a TTY only; yields `update(done, total)` or None."""
    if as_json or not sys.stderr.isatty():
        return contextlib.nullcontext(None)
    prog = Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=Console(stderr=True),
        transient=True,
    )

    @contextlib.contextmanager
    def run() -> Iterator[Callable[[int, int], None]]:
        with prog:
            task = prog.add_task(label)

            def update(done: int, total: int) -> None:
                prog.update(task, completed=done, total=total)

            yield update

    return run()


def status(message: str, as_json: bool) -> contextlib.AbstractContextManager[None]:
    """A spinner on stderr for a TTY only; nothing when --json or not a terminal."""
    if as_json or not sys.stderr.isatty():
        return contextlib.nullcontext(None)

    @contextlib.contextmanager
    def spin() -> Iterator[None]:
        with Console(stderr=True).status(message):
            yield None

    return spin()


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    """Aligned plain-text columns, two spaces apart; the header only when there are rows."""
    if not rows:
        return []
    widths = [max([len(header[i]), *(len(r[i]) for r in rows)]) for i in range(len(header) - 1)]

    def line(cells: list[str]) -> str:
        padded = [cells[i].ljust(widths[i]) for i in range(len(widths))]
        return "  ".join([*padded, cells[-1]]).rstrip()

    return [line(header), *(line(r) for r in rows)]
