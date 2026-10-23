"""mappd workflow: the D layer, durable execution."""

import sys
from dataclasses import asdict
from enum import StrEnum
from typing import Annotated

import typer

from mappd import workflows
from mappd.cli._common import (
    DB_ERRORS,
    EXIT_USAGE,
    JsonFlag,
    emit,
    fail,
    load_settings,
    table,
)
from mappd.settings import Settings, SettingsError


class Status(StrEnum):
    """Workflow statuses as DBOS stores them."""

    PENDING = "PENDING"
    ENQUEUED = "ENQUEUED"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"
    MAX_RECOVERY_ATTEMPTS_EXCEEDED = "MAX_RECOVERY_ATTEMPTS_EXCEEDED"


app = typer.Typer(help="D: durable workflows.", no_args_is_help=True)


@app.callback()
def _processes_importable() -> None:
    """Put the processes package on the import path before any workflow is read.

    DBOS pickles workflow inputs and outputs, and their classes live in `processes.*`: without
    the path, decoding them warns "No module named 'processes'". Only the path, no import, so
    a process that fails to load never stops these commands.
    """
    try:
        root = Settings.from_env().processes_dir.resolve()
    except SettingsError:
        return  # each command reports bad settings itself
    if str(root.parent) not in sys.path:
        sys.path.insert(0, str(root.parent))


@app.command("list")
def list_(
    status: Annotated[
        list[Status] | None,
        typer.Option("--status", case_sensitive=False, help="Filter by status; repeatable."),
    ] = None,
    stranded: Annotated[
        bool,
        typer.Option("--stranded", help="Only PENDING workflows left on an older app version."),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, help="At most this many.")] = 100,
    as_json: JsonFlag = False,
) -> None:
    """List workflows, newest first.

    Example:
        mappd workflow list --stranded --json
    """
    settings = load_settings()
    try:
        statuses = [s.value for s in status] if status else None
        rows = workflows.list_workflows(settings, status=statuses, stranded=stranded, limit=limit)
    except DB_ERRORS as e:
        fail(f"system database unavailable: {e}")
    emit(
        {"workflows": [w.model_dump(mode="json") for w in rows]},
        as_json,
        table(
            ["STATUS", "VERSION", "NAME", "ID"],
            [[w.status, w.app_version or "-", w.name, w.workflow_id] for w in rows],
        )
        or ["no workflows"],
    )


@app.command()
def recover(
    to_version: Annotated[
        str | None,
        typer.Option("--to-version", help="Target version (default: MAPPD_APP_VERSION)."),
    ] = None,
    as_json: JsonFlag = False,
) -> None:
    """Move stranded workflows onto the running version, resuming after their last step.

    Run it after a deploy when `mappd doctor` reports stranded workflows.

    Example:
        mappd workflow recover --json
    """
    settings = load_settings()
    target = to_version or settings.app_version
    if to_version is None and target == "dev":
        fail(
            "MAPPD_APP_VERSION is not set; pass --to-version with the running app's version",
            EXIT_USAGE,
        )
    try:
        moved = workflows.recover(settings, target)
    except workflows.UnknownVersionError as e:
        fail(str(e), EXIT_USAGE)
    except DB_ERRORS as e:
        fail(f"system database unavailable: {e}")
    emit(
        {"to_version": target, "moved": [asdict(m) for m in moved]},
        as_json,
        [
            f"{m.workflow_id} ({m.old_version}) -> {m.new_workflow_id} from step {m.start_step}"
            for m in moved
        ]
        or ["nothing to recover"],
    )


@app.command()
def retry(
    workflow_id: Annotated[str, typer.Argument(help="The ERROR workflow to re-run.")],
    to_version: Annotated[
        str | None,
        typer.Option("--to-version", help="Version the fork runs on (default: its own)."),
    ] = None,
    as_json: JsonFlag = False,
) -> None:
    """Fork an ERROR workflow at its failed step; the running version executes it.

    Completed steps do not repeat; the failed one re-runs. A PENDING workflow is not
    an error to retry — that is what `mappd workflow recover` is for. Safe to re-run:
    a second call names the existing fork instead of forking again.

    Example:
        mappd workflow retry support.ticket:t-1 --json
    """
    settings = load_settings()
    try:
        moved = workflows.retry(settings, workflow_id, to_version=to_version)
    except workflows.WorkflowNotFoundError:
        fail(f"no workflow {workflow_id!r}")
    except (workflows.WorkflowNotFailedError, workflows.UnknownVersionError) as e:
        fail(str(e), EXIT_USAGE)
    except DB_ERRORS as e:
        fail(f"system database unavailable: {e}")
    emit(
        asdict(moved),
        as_json,
        [
            f"{moved.workflow_id}: fork {moved.new_workflow_id} from step "
            f"{moved.start_step} on {moved.old_version}"
        ],
    )


@app.command()
def cancel(
    workflow_id: Annotated[str, typer.Argument(help="The workflow to cancel.")],
    as_json: JsonFlag = False,
) -> None:
    """Cancel one running or queued workflow. Exit 1 if it does not exist, 2 if it finished.

    Example:
        mappd workflow cancel support.ticket:t-1
    """
    settings = load_settings()
    try:
        workflows.cancel(settings, workflow_id)
    except workflows.WorkflowNotFoundError:
        fail(f"no workflow {workflow_id!r}")
    except workflows.WorkflowStateError as e:
        fail(str(e), EXIT_USAGE)
    except DB_ERRORS as e:
        fail(f"system database unavailable: {e}")
    emit(
        {"workflow_id": workflow_id, "status": "cancelled"},
        as_json,
        [f"cancelled {workflow_id}"],
    )


def _ms(value: int | None) -> str:
    return "-" if value is None else f"{value}ms"


@app.command()
def show(
    workflow_id: Annotated[str, typer.Argument(help="The workflow to show.")],
    as_json: JsonFlag = False,
) -> None:
    """Show one workflow: steps, outbox rows, payloads.

    Status, links and timings too. MAPPD_WORKFLOWS_PAYLOADS=off leaves inputs and outputs
    out; errors stay.

    Example:
        mappd workflow show support.ticket:t-1 --json
    """
    settings = load_settings()
    try:
        w = workflows.get(settings, workflow_id)
    except workflows.WorkflowNotFoundError:
        fail(f"no workflow {workflow_id!r}")
    except DB_ERRORS as e:
        fail(f"system database unavailable: {e}")
    lines = [
        f"{w.workflow_id}  {w.status}{'  (stranded)' if w.stranded else ''}",
        f"name {w.name}  version {w.app_version or '-'}  queue {w.queue or '-'}",
        f"created {w.created_at or '-'}  completed {w.completed_at or '-'}",
        *([f"parent {w.parent_id}"] if w.parent_id else []),
        *([f"forked from {w.forked_from}"] if w.forked_from else []),
        *([f"error {w.error}"] if w.error else []),
        *(
            f"#{s.function_id:<3} {s.kind:<11} {s.name:<32} {_ms(s.duration_ms):>9} "
            + (s.error or "ok")
            for s in w.steps
        ),
        f"allowed: {', '.join(w.allowed) or 'nothing'}",
    ]
    emit(w.model_dump(mode="json"), as_json, lines)


@app.command()
def resume(
    workflow_id: Annotated[str, typer.Argument(help="The CANCELLED workflow to continue.")],
    as_json: JsonFlag = False,
) -> None:
    """Continue a CANCELLED workflow under its own ID, after its last recorded step.

    Only a workflow of the running version (MAPPD_APP_VERSION) resumes; an older one wants
    `mappd workflow fork --to-version`. Exit 1 if it does not exist, 2 if it cannot resume.

    Example:
        mappd workflow resume support.ticket:t-1
    """
    settings = load_settings()
    try:
        moved = workflows.resume(settings, workflow_id)
    except workflows.WorkflowNotFoundError:
        fail(f"no workflow {workflow_id!r}")
    except workflows.WorkflowStateError as e:
        fail(str(e), EXIT_USAGE)
    except DB_ERRORS as e:
        fail(f"system database unavailable: {e}")
    emit(asdict(moved), as_json, [f"resumed {workflow_id} from step {moved.start_step}"])


@app.command()
def fork(
    workflow_id: Annotated[str, typer.Argument(help="The finished workflow to run again.")],
    step: Annotated[int, typer.Option("--step", help="The first step that runs again.")],
    to_version: Annotated[
        str | None,
        typer.Option("--to-version", help="Version the fork runs on (default: its own)."),
    ] = None,
    as_json: JsonFlag = False,
) -> None:
    """Run a finished workflow again from --step under a new ID; earlier steps are copied.

    Steps from --step on run again. Actions keyed by a business key find their outbox row
    done and send nothing; actions keyed per workflow (AUTO) send again (`show` lists the
    outbox rows). Refused while the workflow runs or an earlier fork of it still runs.
    Exit 1 if it does not exist, 2 if it cannot be forked.

    Example:
        mappd workflow fork support.ticket:t-1 --step 3 --json
    """
    settings = load_settings()
    try:
        moved = workflows.fork(settings, workflow_id, step, to_version=to_version)
    except workflows.WorkflowNotFoundError:
        fail(f"no workflow {workflow_id!r}")
    except (
        workflows.WorkflowStateError,
        workflows.StepOutOfRangeError,
        workflows.UnknownVersionError,
    ) as e:
        fail(str(e), EXIT_USAGE)
    except DB_ERRORS as e:
        fail(f"system database unavailable: {e}")
    emit(
        asdict(moved),
        as_json,
        [f"{workflow_id}: fork {moved.new_workflow_id} from step {moved.start_step}"],
    )
