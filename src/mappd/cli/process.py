"""mappd process: the P layer, processes."""

import asyncio
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, TypedDict
from uuid import uuid4

import typer
from dbos import DBOS
from pydantic import BaseModel

from mappd import runner, runtime, telemetry, workflows
from mappd.app import discover, load
from mappd.autonomy import PolicyError, set_level, set_shadow
from mappd.cli import serve as serve_module
from mappd.cli._common import (
    DB_ERRORS,
    EXIT_USAGE,
    JsonFlag,
    emit,
    fail,
    load_settings,
    status,
)
from mappd.process import Process, get_process

app = typer.Typer(help="P: processes.", no_args_is_help=True)


@app.command()
def seed(
    name: Annotated[str, typer.Argument(help="The process to seed.")],
    as_json: JsonFlag = False,
) -> None:
    """Load a process's base data into the dev database (knowledge base, ...).

    Calls the `seed` function registered on the process's `Process(seed=...)`; it returns
    how many it seeded, which the command prints with `Process(seeds=...)` ("rows" by default).

    Example:
        mappd process seed support --json
    """
    settings = load_settings()
    if name not in discover(settings.processes_dir):
        fail(f"no process {name!r} in {settings.processes_dir}", EXIT_USAGE)
    seeded = serve_module.run_seed(settings, name)
    proc = get_process(settings.processes_dir, name)
    what = proc.seeds if proc is not None else "rows"
    emit({"process": name, "seeded": seeded}, as_json, [f"seeded {name}: {seeded} {what}"])


@app.command("list")
def list_(as_json: JsonFlag = False) -> None:
    """List the processes under MAPPD_PROCESSES_DIR.

    Example:
        mappd process list --json
    """
    settings = load_settings()
    procs = load(settings)
    policies = {name: _policy_row(proc) for name, proc in procs.items()}
    rows = [
        {
            "name": name,
            "api": proc.router is not None,
            "migrations": (
                len(list(proc.migrations_dir.glob("*.py")))
                if proc.migrations_dir is not None and proc.migrations_dir.is_dir()
                else 0
            ),
            **policies[name],
        }
        for name, proc in sorted(procs.items())
    ]
    emit(
        {"processes": rows},
        as_json,
        [
            f"{name:<20} api={'yes' if row['api'] else 'no'}  migrations={row['migrations']}"
            + _policy_text(policies[name])
            for (name, _), row in zip(sorted(procs.items()), rows, strict=True)
        ]
        or ["no processes yet; copy a demo from the repository's processes/"],
    )


@app.command()
def run(
    name: Annotated[str, typer.Argument(help="The process to run.")],
    input_file: Annotated[
        Path, typer.Option("--input", help="JSON file with one case, matching the process INPUT.")
    ],
    as_json: JsonFlag = False,
) -> None:
    """Run one case through a process here and now; prints the result and its trace id.

    Uses the databases from MAPPD_* settings; apply migrations first (mappd db migrate).

    Example:
        mappd process run support --input case.json --json
    """
    settings = load_settings()
    try:
        data = json.loads(input_file.read_text())
    except (OSError, json.JSONDecodeError) as e:
        fail(f"cannot read {input_file}: {e}", EXIT_USAGE)
    try:
        entry, case = runner.prepare(settings, name, data)
    except runner.RunError as e:
        fail(str(e), EXIT_USAGE)
    if settings.app_version == "dev":
        try:
            served = [v for v in workflows.registered_versions(settings) if v != "dev"]
        except DB_ERRORS as e:
            fail(f"system database unavailable: {e}")
        if served:
            # Running as `dev` would register it as the newest version of a live database.
            fail(
                f"MAPPD_APP_VERSION is not set, but this database serves {', '.join(served)}; "
                "set MAPPD_APP_VERSION to the running version",
                EXIT_USAGE,
            )
    provider = telemetry.setup(settings)
    runtime.configure(settings, executor_id=f"{workflows.CLI_EXECUTOR_PREFIX}{uuid4().hex[:12]}")
    DBOS.launch()
    try:
        with status(f"running {name}…", as_json):
            result = asyncio.run(runner.run_once(name, entry, case))
    except Exception as e:  # the process's own failure, reported, not a traceback
        fail(f"{name} failed: {type(e).__name__}: {e}")
    finally:
        DBOS.destroy()
        provider.shutdown()
    output = (
        result.output.model_dump(mode="json")
        if isinstance(result.output, BaseModel)
        else result.output
    )
    traces = f"{settings.mlflow_url}/#/experiments/{settings.mlflow_experiment_id}/traces"
    emit(
        {
            "workflow_id": result.workflow_id,
            "output": output,
            "trace_id": result.trace_id,
            "traces": traces,
        },
        as_json,
        [
            json.dumps(output, indent=2),
            f"workflow {result.workflow_id}",
            f"trace {result.trace_id}  {traces}",
        ],
    )


class OnOff(StrEnum):
    """Shadow mode on or off."""

    on = "on"
    off = "off"


class LevelArg(StrEnum):
    """An autonomy level."""

    auto = "auto"
    review = "review"


def _policy_target(name: str) -> tuple[Process, Path]:
    settings = load_settings()
    proc = load(settings).get(name)
    if proc is None:
        fail(f"no process {name!r} in {settings.processes_dir}", EXIT_USAGE)
    path = proc.autonomy_path()
    if path is None:
        fail(f"process {name!r} declares no autonomy file", EXIT_USAGE)
    return proc, path


def _report(path: Path, diff: str, as_json: bool) -> None:
    lines = (
        [
            diff.rstrip("\n"),
            f"the app reads {path} at start: restart `mappd serve` to apply it here,"
            " and commit it to apply it on the next deploy",
        ]
        if diff
        else [f"{path}: unchanged"]
    )
    emit({"path": str(path), "changed": bool(diff), "diff": diff}, as_json, lines)


@app.command()
def shadow(
    name: Annotated[str, typer.Argument(help="The process.")],
    state: Annotated[
        OnOff,
        typer.Argument(help="on: record decisions, send nothing; off: act per autonomy.yaml."),
    ],
    as_json: JsonFlag = False,
) -> None:
    """Turn shadow mode on or off in the process's autonomy.yaml (a diff to commit).

    Example:
        mappd process shadow support on
    """
    _, path = _policy_target(name)
    try:
        diff = set_shadow(path, state is OnOff.on)
    except PolicyError as e:
        fail(str(e), EXIT_USAGE)
    _report(path, diff, as_json)


@app.command()
def autonomy(
    name: Annotated[str, typer.Argument(help="The process.")],
    category: Annotated[str, typer.Argument(help="A category of the process's output.")],
    level: Annotated[
        LevelArg,
        typer.Argument(help="auto: send without approval; review: a person approves first."),
    ],
    as_json: JsonFlag = False,
) -> None:
    """Set one category's autonomy level in autonomy.yaml (a diff to commit).

    Escalations and the process's hard rules always go to a person, whatever the level.

    Example:
        mappd process autonomy support how_to auto
    """
    proc, path = _policy_target(name)
    try:
        diff = set_level(path, category, level.value, allowed=proc.categories)
    except ValueError as e:
        fail(str(e), EXIT_USAGE)
    _report(path, diff, as_json)


class PolicyRow(TypedDict):
    """A process's policy as `process list` shows it; shadow is None without a policy file."""

    shadow: bool | None
    auto: list[str]


def _policy_row(proc: Process) -> PolicyRow:
    if proc.autonomy is None:
        return {"shadow": None, "auto": []}
    try:
        p = proc.policy()
    except PolicyError:  # `mappd doctor` names the broken file; list still lists
        return {"shadow": None, "auto": []}
    return {"shadow": p.shadow, "auto": sorted(c for c, lv in p.categories.items() if lv == "auto")}


def _policy_text(row: PolicyRow) -> str:
    if row["shadow"] is None:
        return ""
    return f"  shadow={'on' if row['shadow'] else 'off'}  auto={','.join(row['auto']) or '-'}"
