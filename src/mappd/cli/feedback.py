"""mappd feedback: operator decisions → eval cases."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from mappd.app import load
from mappd.cli._common import (
    DB_ERRORS,
    EXIT_USAGE,
    JsonFlag,
    emit,
    fail,
    load_settings,
    status,
)
from mappd.feedback import sync as sync_feedback

app = typer.Typer(help="Operator decisions as eval cases.", no_args_is_help=True)
SINCE = re.compile(r"^(\d+)([dh])$")


def _since(value: str) -> datetime:
    m = SINCE.match(value)
    if m is None:
        fail(f"--since must look like 30d or 12h, not {value!r}", EXIT_USAGE)
    n, unit = int(m.group(1)), m.group(2)
    return datetime.now(UTC) - (timedelta(days=n) if unit == "d" else timedelta(hours=n))


@app.command()
def sync(
    name: Annotated[str, typer.Argument(help="The process, e.g. support.")],
    since: Annotated[str, typer.Option(help="How far back: 30d, 12h.")] = "30d",
    out: Annotated[
        Path | None,
        typer.Option(help="Write here instead of evals/feedback.jsonl (keep it git-ignored)."),
    ] = None,
    as_json: JsonFlag = False,
) -> None:
    """Turn decisions people made into cases in evals/feedback.jsonl (git-ignored).

    One case per ticket; a re-sync replaces changed cases. Run them with
    `mappd eval run <name> --set feedback`.

    Example:
        mappd feedback sync support --since 7d --json
    """
    start = _since(since)
    settings = load_settings()
    proc = load(settings).get(name)
    if proc is None:
        fail(f"no process {name!r} in {settings.processes_dir}", EXIT_USAGE)
    if proc.feedback is None:
        fail(f"process {name!r} declares no feedback", EXIT_USAGE)
    try:
        with status("syncing feedback…", as_json):
            report = asyncio.run(
                sync_feedback(proc, settings.processes_dir / name / "evals", start, out=out)
            )
    except DB_ERRORS as e:
        fail(f"app database unavailable: {e}")
    emit(
        report.model_dump(),
        as_json,
        [
            f"{report.path}: {report.new} new, {report.updated} updated, "
            f"{report.unchanged} unchanged ({report.edited} edited)",
            f"run them: mappd eval run {name} --set feedback",
        ],
    )
