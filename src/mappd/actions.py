"""Side effects as durable steps with an idempotency key and an outbox.

DBOS runs a step at least once: a crash after the side effect but before the checkpoint re-runs
it. So every action that changes the outside world (AGENTS.md, rule 2) goes
through the `actions` table in three short transactions:

1. claim: record the intent and mark it `sending`, committed before acting;
2. act: call the function with no transaction open; it passes the idempotency key on to
   receivers that honor one;
3. record: `done` with the result, `failed` with the error, or `unknown`.

A re-run reads the row and knows what happened. `done` returns the stored result without acting;
`failed` acts again; `sending` left by the same workflow (or older than the lease) means an
earlier attempt died mid-call, so its outcome is unknown, and the action's `on_unknown` policy
decides: "resend" when the receiver deduplicates by key, "escalate" for keyless receivers (the
action becomes `unknown` and stops until a person settles it with `resolve`). A function that
cannot tell whether its call took effect (a timeout after sending) raises OutcomeUnknownError.

A mutating Pydantic AI tool calls an @action function; tools are not DBOS steps by themselves.

Example:
    >>> @action(on_unknown="escalate")  # named "support.send_email" from its module
    ... def send_email(idempotency_key: str, to: str, body: str) -> dict[str, Any]:
    ...     return smtp.send(to=to, body=body)
"""

import asyncio
import functools
import inspect
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Concatenate, Literal, cast, overload

import sqlalchemy as sa
from dbos import DBOS
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mappd.db import Connection, connect, run
from mappd.db.tables import ACTIONS
from mappd.naming import step_name

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

KEY = "idempotency_key"
LEASE_SECONDS = 300.0

NAMES: set[str] = set()
"""Every action's step name, registered when the decorator runs (the dashboard's step kinds)."""

AUTO: str = cast("str", object())
"""Pass as `idempotency_key` to deduplicate by step: "<workflow_id>:<action>:<step_id>".

A retry or a recovered execution of the same step finds its own record and does not act
twice. A different workflow gets a different key — when the effect must deduplicate
across workflows, pass `per(entity)` or a business key.
"""

# What a person does with an unknown outcome, as the error that reports it says.
SETTLE = (
    "check with the receiver, then settle it at /actions?status=unknown"
    " (or `mappd action resolve`); settling also retries the workflow"
)


class _Per(str):
    """The marker `per(entity)` returns: the entity id, resolved to a real key in the step."""


def per(entity: str) -> str:
    """An idempotency key for one entity of this action: "<process>.<action>:<entity>".

    The action supplies its own namespaced name; the caller supplies only what it knows —
    the entity the effect belongs to. Any workflow touching the entity finds the same
    record and does not act twice (a reply per ticket, whatever the approval attempt).
    """
    if not isinstance(entity, str) or not entity:
        raise ValueError("per needs a non-empty entity id")
    return _Per(entity)


type Result = dict[str, Any]
type OnUnknown = Literal["resend", "escalate"]


class OutcomeUnknownError(RuntimeError):
    """The action may or may not have taken effect; a person must settle it with `resolve`."""


class PermanentError(RuntimeError):
    """The receiver refused for good (a rejected recipient): retrying cannot help."""


class ActionInProgressError(RuntimeError):
    """Another workflow is performing this action right now.

    DBOS retries it with the step's backoff (about 15 s with the defaults). A loser still
    blocked after that ends in ERROR rather than waiting out the lease: duplicate workflows on
    one key are rare, because workflow IDs already collapse redeliveries and double clicks.
    """


def _dumps(value: object) -> str:
    return json.dumps(value, default=str)


def _as_json(value: Mapping[str, Any]) -> Result:
    """`value` as it reads back from jsonb, so first runs and replays look the same."""
    normalized: Result = json.loads(_dumps(dict(value)))
    return normalized


def perform(
    conn: Connection,
    kind: str,
    key: str,
    payload: Mapping[str, Any],
    fn: Callable[[], Result],
    *,
    on_unknown: OnUnknown = "resend",
    claimed_by: str | None = None,
    lease_seconds: float = LEASE_SECONDS,
) -> Result:
    """Run `fn` for `key` until it is done once, keeping the action's state in `actions`.

    `fn` acts again only after a recorded failure, or after an interrupted attempt under the
    "resend" policy. The row keeps the latest state, the number of claims and the last error.

    `claimed_by` names the caller (the DBOS workflow ID): a `sending` row with the same name is
    this caller's own interrupted attempt. Results come back as stored JSON, on first runs too.

    Raises:
        ValueError: `key` was already used by another kind or with a different payload.
        TypeError: `fn` returned something other than a dict.
        OutcomeUnknownError: the action's outcome is unknown and must be resolved by a person.
        ActionInProgressError: another caller holds a live claim on `key`.
    """
    stored = _claim(
        conn,
        kind,
        key,
        payload,
        on_unknown=on_unknown,
        claimed_by=claimed_by,
        lease_seconds=lease_seconds,
    )
    if stored is not None:
        return stored
    try:
        result: object = fn()  # checked below: fn is user code
    except OutcomeUnknownError as e:
        _record(conn, key, "unknown", error=str(e))
        raise
    except Exception as e:
        _record(conn, key, "failed", error=f"{type(e).__name__}: {e}")
        raise
    if not isinstance(result, dict):
        _record(conn, key, "failed", error="the action did not return a JSON object")
        raise TypeError(f"{kind}: an action must return a JSON object, got {type(result).__name__}")
    stored_result = _as_json(result)
    _record(conn, key, "done", result=stored_result)
    return stored_result


def _claim(
    conn: Connection,
    kind: str,
    key: str,
    payload: Mapping[str, Any],
    *,
    on_unknown: OnUnknown,
    claimed_by: str | None,
    lease_seconds: float,
) -> Result | None:
    """Claim `key` for this attempt; returns the stored result if the action is done."""
    escalate = False
    stale = sa.func.coalesce(
        sa.func.now() - ACTIONS.c.claimed_at
        > sa.func.make_interval(0, 0, 0, 0, 0, 0, sa.bindparam("lease")),
        sa.false(),
    ).label("stale")
    with conn.transaction():
        run(
            conn,
            pg_insert(ACTIONS)
            .values(kind=kind, idempotency_key=key, payload=Jsonb(dict(payload), dumps=_dumps))
            .on_conflict_do_nothing(index_elements=["idempotency_key"]),
        )
        row = run(
            conn,
            sa.select(
                ACTIONS.c.kind,
                ACTIONS.c.status,
                ACTIONS.c.payload,
                ACTIONS.c.result,
                ACTIONS.c.claimed_by,
                stale,
            )
            .where(ACTIONS.c.idempotency_key == key)
            .with_for_update(),
            {"lease": lease_seconds},
        ).fetchone()
        if row is None:  # pragma: no cover - the insert above guarantees a row
            raise RuntimeError(f"action {key!r} vanished")
        if row["kind"] != kind:
            raise ValueError(f"idempotency key {key!r} is already used by {row['kind']}")
        if row["payload"] != _as_json(payload):
            raise ValueError(
                f"{kind}: idempotency key {key!r} was already used with a different payload"
            )
        status = row["status"]
        if status == "done":
            stored: Result = row["result"]
            return stored
        if status == "unknown":
            raise OutcomeUnknownError(f"{kind} {key!r}: outcome unknown; {SETTLE}")
        if status == "sending":
            own = claimed_by is not None and row["claimed_by"] == claimed_by
            if not own and not row["stale"]:
                holder = row["claimed_by"] or "another caller"
                raise ActionInProgressError(f"{kind} {key!r} is being performed by {holder}")
            escalate = on_unknown == "escalate"
        if escalate:
            run(
                conn,
                sa.update(ACTIONS)
                .where(ACTIONS.c.idempotency_key == key)
                .values(
                    status="unknown",
                    updated_at=sa.func.now(),
                    last_error="an earlier attempt stopped mid-call",
                ),
            )
        else:
            run(
                conn,
                sa.update(ACTIONS)
                .where(ACTIONS.c.idempotency_key == key)
                .values(
                    status="sending",
                    attempts=ACTIONS.c.attempts + 1,
                    claimed_by=claimed_by,
                    claimed_at=sa.func.now(),
                    last_error=None,
                    updated_at=sa.func.now(),
                ),
            )
    if escalate:
        raise OutcomeUnknownError(f"{kind} {key!r}: an earlier attempt stopped mid-call; {SETTLE}")
    return None


def _record(
    conn: Connection,
    key: str,
    status: str,
    *,
    result: Result | None = None,
    error: str | None = None,
) -> None:
    run(
        conn,
        sa.update(ACTIONS)
        .where(ACTIONS.c.idempotency_key == key)
        .values(
            status=status,
            result=None if result is None else Jsonb(result),
            last_error=error,
            claimed_at=None,
            updated_at=sa.func.now(),
        ),
    )


def resolve(
    conn: Connection,
    key: str,
    *,
    took_effect: bool,
    operator: str,
    result: Mapping[str, Any] | None = None,
) -> None:
    """Settle an `unknown` action after checking with the receiver; `operator` is recorded.

    `took_effect=True` marks it done with `result`; False makes it pending, so the next run acts.

    Raises:
        LookupError: no action with this key is `unknown`.
    """
    status, stored = ("done", Jsonb(_as_json(result or {}))) if took_effect else ("pending", None)
    row = run(
        conn,
        sa.update(ACTIONS)
        .where(ACTIONS.c.idempotency_key == key, ACTIONS.c.status == "unknown")
        .values(
            status=status,
            result=stored,
            last_error=None,
            settled_by=operator,
            updated_at=sa.func.now(),
        )
        .returning(ACTIONS.c.id),
    ).fetchone()
    if row is None:
        raise LookupError(f"no unknown action with idempotency key {key!r}")


STATUSES = ("pending", "sending", "done", "failed", "unknown")


class ActionRow(BaseModel):
    """One row of the outbox as operators see it."""

    idempotency_key: str
    kind: str
    payload: dict[str, Any]
    status: str
    result: dict[str, Any] | None
    last_error: str | None
    attempts: int
    claimed_by: str | None = Field(description="the workflow that ran it last; retry that one")
    settled_by: str | None = Field(default=None, description="who settled it, if it was unknown")
    created_at: datetime
    updated_at: datetime


def list_actions(
    conn: Connection,
    *,
    status: str | None = None,
    kind: str | None = None,
    key: str | None = None,
    claimed_by: str | None = None,
    limit: int = 100,
) -> list[ActionRow]:
    """Actions, most recently changed first; `status`, `kind`, `key`, `claimed_by` filter."""
    query = sa.select(ACTIONS).order_by(ACTIONS.c.updated_at.desc()).limit(limit)
    if status is not None:
        query = query.where(ACTIONS.c.status == status)
    if kind is not None:
        query = query.where(ACTIONS.c.kind == kind)
    if key is not None:
        query = query.where(ACTIONS.c.idempotency_key == key)
    if claimed_by is not None:
        query = query.where(ACTIONS.c.claimed_by == claimed_by)
    return [ActionRow.model_validate(dict(r)) for r in run(conn, query).fetchall()]


def retryable(error: BaseException) -> bool:
    """Whether DBOS should retry a failed action: not for bugs, refusals or unknown outcomes."""
    return not isinstance(error, (ValueError, TypeError, OutcomeUnknownError, PermanentError))


@overload
def action[**P](
    kind: Callable[Concatenate[str, P], Result],
    *,
    max_attempts: int = 5,
    on_unknown: OnUnknown = "resend",
) -> Callable[Concatenate[str, P], Coroutine[Any, Any, Result]]: ...


@overload
def action[**P](
    kind: str | None = None,
    *,
    max_attempts: int = 5,
    on_unknown: OnUnknown = "resend",
) -> Callable[
    [Callable[Concatenate[str, P], Result]],
    Callable[Concatenate[str, P], Coroutine[Any, Any, Result]],
]: ...


@overload
def action[**P](
    kind: str | None = None,
    *,
    per: str,
    max_attempts: int = 5,
    on_unknown: OnUnknown = "resend",
) -> Callable[
    [Callable[Concatenate[str, P], Result]],
    Callable[P, Coroutine[Any, Any, Result]],
]: ...


def action[**P](
    kind: str | Callable[Concatenate[str, P], Result] | None = None,
    *,
    per: str | None = None,
    max_attempts: int = 5,
    on_unknown: OnUnknown = "resend",
) -> (
    Callable[Concatenate[str, P], Coroutine[Any, Any, Result]]
    | Callable[
        [Callable[Concatenate[str, P], Result]],
        Callable[Concatenate[str, P], Coroutine[Any, Any, Result]],
    ]
    | Callable[[Callable[Concatenate[str, P], Result]], Callable[P, Coroutine[Any, Any, Result]]]
):
    """Make `fn` a side-effect step: a DBOS step with retries, deduplicated by its key.

    Bare — `@action` — the step is named "<process>.<function>", derived from the module
    (mappd.naming.step_name); `action("name")` pins a name for journal compatibility.

    The decorated function's first parameter must be `idempotency_key`; it receives the
    resolved key to pass on to the receiver. Callers supply it as the first argument:
    `AUTO` deduplicates the step inside its workflow, `per(entity)` one entity across
    workflows, a plain string is a business key. With `@action(per="param")` the key is
    "<action>:<param>" from the named argument instead — callers pass business arguments
    only. `on_unknown` is "resend" when the receiver honors the key, "escalate" when it
    cannot deduplicate. Callers `await` the step: the sync outbox machinery runs on a
    worker thread so the event loop never blocks.
    """

    def decorate(
        fn: Callable[Concatenate[str, P], Result],
    ) -> Callable[Concatenate[str, P], Coroutine[Any, Any, Result]]:
        resolved = kind if isinstance(kind, str) else step_name(fn)
        NAMES.add(resolved)
        signature = inspect.signature(fn)
        parameters = list(signature.parameters.values())
        if not parameters or parameters[0].name != KEY:
            raise TypeError(f"{fn.__qualname__}: an action's first parameter must be `{KEY}`")
        call_signature = signature
        if per is not None:
            if per not in signature.parameters or per == KEY:
                raise TypeError(
                    f"{fn.__qualname__}: `per` must name one of the function's arguments"
                )
            call_signature = signature.replace(parameters=parameters[1:])

        @functools.wraps(fn)
        async def run(*args: P.args, **kwargs: P.kwargs) -> Result:
            bound = call_signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            if per is not None:
                entity = arguments.get(per)
                if not isinstance(entity, str) or not entity:
                    raise ValueError(f"{resolved}: `{per}` must be a non-empty str, got {entity!r}")
                key = f"{resolved}:{entity}"
            else:
                marker = arguments.get(KEY)
                if isinstance(marker, _Per):
                    key = f"{resolved}:{marker}"
                elif marker is AUTO:
                    workflow_id, step_id = DBOS.workflow_id, DBOS.step_id
                    if workflow_id is None or step_id is None:
                        raise ValueError(
                            f"{resolved}: AUTO needs a step inside a workflow;"
                            " pass an explicit idempotency_key"
                        )
                    key = f"{workflow_id}:{resolved}:{step_id}"
                elif isinstance(marker, str) and marker:
                    key = marker
                else:
                    raise ValueError(f"{resolved}: the idempotency key must be a non-empty str")
            invoke = {KEY: key, **{k: v for k, v in arguments.items() if k != KEY}}
            payload = dict(invoke)
            payload.pop(KEY)
            call = cast("Callable[..., Result]", fn)

            def perform_sync() -> Result:
                with connect() as conn:
                    return perform(
                        conn,
                        resolved,
                        key,
                        payload,
                        lambda: call(**invoke),
                        on_unknown=on_unknown,
                        claimed_by=DBOS.workflow_id,
                    )

            return await asyncio.to_thread(perform_sync)

        step = DBOS.step(
            name=resolved, retries_allowed=True, max_attempts=max_attempts, should_retry=retryable
        )(run)
        return cast("Callable[Concatenate[str, P], Coroutine[Any, Any, Result]]", step)

    return decorate(kind) if callable(kind) else decorate
