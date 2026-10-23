"""What the workflow dashboard shows: models and pure helpers, no DBOS calls and no I/O.

`mappd.workflows` fills these models from the DBOS client; the API returns them and the pages
render them. Payloads — workflow inputs, outputs
and step outputs — carry customer text: they are cut to `PAYLOAD_LIMIT` characters, and with
`MAPPD_WORKFLOWS_PAYLOADS=off` the service leaves them out (errors stay).
"""

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from mappd import actions
from mappd.db import transaction

if TYPE_CHECKING:
    from collections.abc import Mapping

FINISHED = ("SUCCESS", "ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED")
IN_FLIGHT = ("PENDING", "ENQUEUED")
# A `mappd process run` executor's ID prefix (its PENDING workflows strand when it dies).
CLI_EXECUTOR_PREFIX = "mappd-cli-"
PAYLOAD_LIMIT = 20_000
SUMMARY_LIMIT = 200

type StepKind = Literal["action", "transaction", "child", "step"]


class Payload(BaseModel):
    """A value as text, cut to `PAYLOAD_LIMIT` characters."""

    text: str
    truncated: bool


class ActionRef(BaseModel):
    """An outbox row a workflow claimed; `per_workflow` keys (AUTO) send again in a fork."""

    idempotency_key: str
    kind: str
    status: str
    per_workflow: bool


class Step(BaseModel):
    """One journaled step."""

    function_id: int
    name: str
    kind: StepKind
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    error: str | None
    child_workflow_id: str | None
    output: Payload | None
    outbox: ActionRef | None = None


class WorkflowSummary(BaseModel):
    """One workflow in a list: no payloads."""

    workflow_id: str
    name: str
    process: str
    status: str
    stranded: bool
    app_version: str | None
    queue: str | None
    created_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    parent_id: str | None
    forked_from: str | None
    error_summary: str | None


class WorkflowDetail(WorkflowSummary):
    """One workflow with its links, steps, outbox rows and (when on) payloads."""

    executor_id: str | None
    dequeued_at: datetime | None
    recovery_attempts: int | None
    children: list[str]
    forks: list[str]
    root_workflow_id: str
    input: Payload | None
    output: Payload | None
    error: str | None
    steps: list[Step]
    actions: list[ActionRef]
    payloads: bool
    allowed: list[str]


class QueueDepth(BaseModel):
    """In-flight workflows on one queue."""

    name: str
    waiting: int
    running: int


class WorkflowStats(BaseModel):
    """Counts for the dashboard's tiles."""

    since: datetime
    by_status: dict[str, int]
    stranded: int
    by_name: dict[str, int]
    queues: list[QueueDepth]
    processes: list[str]


def payload(value: object) -> Payload:
    """`value` as text: a string as itself, an exception as "Type: message", the rest as JSON."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, BaseException):
        text = f"{type(value).__name__}: {value}"
    else:
        try:
            jsonable = to_jsonable_python(value, fallback=repr)
        except ValueError:  # bytes that are not UTF-8, and other values pydantic refuses
            jsonable = repr(value)
        text = json.dumps(jsonable, indent=2, ensure_ascii=False, default=repr)
    return Payload(text=text[:PAYLOAD_LIMIT], truncated=len(text) > PAYLOAD_LIMIT)


def error_text(error: object) -> str | None:
    """An error as one string; None stays None."""
    if error is None:
        return None
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"
    return str(error)


def summary(text: str | None) -> str | None:
    """The first line, at most `SUMMARY_LIMIT` characters; None for nothing."""
    if not text:
        return None
    return text.splitlines()[0][:SUMMARY_LIMIT] or None


def ms(epoch_ms: int | None) -> datetime | None:
    """DBOS epoch milliseconds as a UTC datetime."""
    return None if epoch_ms is None else datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)


def duration(start_ms: int | None, end_ms: int | None) -> int | None:
    """Milliseconds between two DBOS times; None unless both are known."""
    if start_ms is None or end_ms is None:
        return None
    return max(0, end_ms - start_ms)


def process_of(name: str) -> str:
    """The process a workflow or step belongs to: names are "<process>.<function>"."""
    process, dot, _ = name.partition(".")
    return process if dot else ""


def step_kind(name: str, child_workflow_id: str | None) -> StepKind:
    """What a step is, for the step list and the fork confirmation."""
    if child_workflow_id:
        return "child"
    if name in actions.NAMES:
        return "action"
    if name in transaction.TRANSACTIONS:
        return "transaction"
    return "step"


def step_from(info: Mapping[str, Any], *, payloads: bool) -> Step:
    """A `Step` from a DBOS `StepInfo`; the output only when `payloads`."""
    started, completed = info.get("started_at_epoch_ms"), info.get("completed_at_epoch_ms")
    child = info.get("child_workflow_id")
    name = str(info["function_name"])
    return Step(
        function_id=int(info["function_id"]),
        name=name,
        kind=step_kind(name, child),
        started_at=ms(started),
        completed_at=ms(completed),
        duration_ms=duration(started, completed),
        error=error_text(info.get("error")),
        child_workflow_id=child,
        output=payload(info.get("output")) if payloads and info.get("error") is None else None,
    )


def allowed(status: str, *, stranded: bool, current_version: bool) -> list[str]:
    """The actions a workflow's status allows, in button order."""
    if status in IN_FLIGHT:
        return ["cancel", "recover"] if stranded else ["cancel"]
    if status == "ERROR":
        return ["retry", "fork"]
    if status == "SUCCESS":
        return ["fork"]
    if status in FINISHED:
        return ["resume", "fork"] if current_version else ["fork"]
    return []
