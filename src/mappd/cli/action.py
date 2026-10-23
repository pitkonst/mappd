"""mappd action: the outbox — list actions, settle the ones whose outcome is unknown."""

import getpass
import json
from enum import StrEnum
from typing import Annotated, Any

import typer

from mappd import actions
from mappd.cli._common import (
    DB_ERRORS,
    EXIT_USAGE,
    JsonFlag,
    emit,
    fail,
    load_settings,
    table,
)
from mappd.db import connect


class ActionStatus(StrEnum):
    """Action statuses as the outbox stores them (mappd.actions.STATUSES)."""

    pending = "pending"
    sending = "sending"
    done = "done"
    failed = "failed"
    unknown = "unknown"


app = typer.Typer(help="D: the side-effect outbox.", no_args_is_help=True)


@app.command("list")
def list_(
    status: Annotated[ActionStatus | None, typer.Option(case_sensitive=False)] = None,
    kind: Annotated[str | None, typer.Option(help="Action name, e.g. support.send_email.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500, help="At most this many.")] = 100,
    as_json: JsonFlag = False,
) -> None:
    """List actions, most recently changed first.

    Example:
        mappd action list --status unknown --json
    """
    settings = load_settings()
    try:
        with connect(settings) as conn:
            rows = actions.list_actions(
                conn, status=status.value if status else None, kind=kind, limit=limit
            )
    except DB_ERRORS as e:
        fail(f"app database unavailable: {e}")
    emit(
        {"actions": [r.model_dump(mode="json") for r in rows]},
        as_json,
        table(
            ["STATUS", "KIND", "KEY", "ERROR"],
            [[r.status, r.kind, r.idempotency_key, r.last_error or ""] for r in rows],
        )
        or ["no actions"],
    )


@app.command()
def resolve(
    key: Annotated[str, typer.Argument(help="The action's idempotency key.")],
    took_effect: Annotated[
        bool, typer.Option("--took-effect", help="The receiver acted: mark done.")
    ] = False,
    not_sent: Annotated[
        bool,
        typer.Option("--not-sent", help="The receiver did not act: mark pending to send again."),
    ] = False,
    result: Annotated[
        str | None, typer.Option(help="JSON result to store with --took-effect.")
    ] = None,
    operator: Annotated[
        str, typer.Option(help="Who checked with the receiver; recorded on the action.")
    ] = getpass.getuser(),
    as_json: JsonFlag = False,
) -> None:
    """Settle an `unknown` action after checking with the receiver.

    --took-effect marks it done (with --result); --not-sent makes it pending, so the next
    run of its workflow (`mappd workflow retry`) acts again. The page at /actions settles and
    retries in one step.

    Example:
        mappd action resolve 'support.send_email:<m-1@example.com>' --not-sent
    """
    if took_effect == not_sent:
        fail("pass exactly one of --took-effect and --not-sent", EXIT_USAGE)
    if result is not None and not took_effect:
        fail("--result goes with --took-effect", EXIT_USAGE)
    try:
        parsed: dict[str, Any] | None = json.loads(result) if result is not None else None
    except json.JSONDecodeError as e:
        fail(f"--result is not JSON: {e}", EXIT_USAGE)
    if parsed is not None and not isinstance(parsed, dict):
        fail("--result must be a JSON object", EXIT_USAGE)
    settings = load_settings()
    try:
        with connect(settings) as conn:
            actions.resolve(conn, key, took_effect=took_effect, operator=operator, result=parsed)
    except LookupError as e:
        fail(str(e))
    except DB_ERRORS as e:
        fail(f"app database unavailable: {e}")
    status = "done" if took_effect else "pending"
    emit({"idempotency_key": key, "status": status}, as_json, [f"{key}: {status}"])
