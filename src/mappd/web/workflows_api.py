"""Core API: durable workflows for operators and integrators (`/api/v1/workflows`).

A thin adapter over `mappd.workflows`, the service layer the CLI and the pages share; every rule
lives there. The DBOS client is synchronous, so calls run on a worker thread with the app's
shared client.
"""

import asyncio
import dataclasses
from contextlib import contextmanager
from datetime import datetime  # noqa: TC003 - FastAPI reads the annotation at runtime
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from mappd import telemetry, workflows
from mappd.settings import Settings
from mappd.workflow_view import FINISHED, IN_FLIGHT, WorkflowDetail, WorkflowStats, WorkflowSummary

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])

STATUSES = (*IN_FLIGHT, *FINISHED)
type Status = Literal[
    "PENDING",
    "ENQUEUED",
    "SUCCESS",
    "ERROR",
    "CANCELLED",
    "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
    "stranded",
]
type Window = Literal["1h", "24h", "7d"]
type Action = Literal["cancel", "resume", "retry", "fork"]


class RetryIn(BaseModel):
    """Retry at the failed step, on `to_version` (default: the workflow's own)."""

    to_version: str | None = None


class ForkIn(BaseModel):
    """Fork from `start_step`, on `to_version` (default: the workflow's own)."""

    start_step: int
    to_version: str | None = None


class RecoverIn(BaseModel):
    """Recover stranded workflows onto `to_version`; only `workflow_ids` when given."""

    to_version: str
    workflow_ids: list[str] | None = None


class Moved(BaseModel):
    """What an action did: the workflow, the one now running it, and from which step."""

    workflow_id: str
    old_version: str | None
    new_workflow_id: str
    start_step: int


class TraceOut(BaseModel):
    """The MLflow trace of a workflow's root, or the experiment's trace list."""

    trace_id: str | None
    url: str


@dataclasses.dataclass(frozen=True)
class Filters:
    """The list's filters, shared by the API and the pages."""

    status: list[str] | None = None
    process: str | None = None
    name: str | None = None
    version: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    id_prefix: str | None = None
    limit: int = 50
    offset: int = 0
    scheduled: bool = True


def settings_of(request: Request) -> Settings:
    """The app's settings (`create_app` stores them), else the environment's."""
    found = getattr(request.app.state, "settings", None)
    return found if isinstance(found, Settings) else Settings.from_env()


@contextmanager
def service_errors() -> Iterator[None]:
    """The service's refusals as HTTP answers; database errors reach `web.errors` (503)."""
    try:
        yield
    except workflows.WorkflowNotFoundError as e:
        raise HTTPException(404, f"no workflow {e.args[0]!r}") from e
    except workflows.WorkflowStateError as e:
        raise HTTPException(409, str(e)) from e
    except (workflows.StepOutOfRangeError, workflows.UnknownVersionError) as e:
        raise HTTPException(422, str(e)) from e


async def list_rows(settings: Settings, filters: Filters) -> list[WorkflowSummary]:
    """The list the API and the page share."""
    statuses = filters.status or []
    return await asyncio.to_thread(
        workflows.list_workflows,
        settings,
        status=[s for s in statuses if s != "stranded"] or None,
        stranded="stranded" in statuses,
        process=filters.process,
        name=filters.name,
        version=filters.version,
        since=filters.since,
        until=filters.until,
        id_prefix=filters.id_prefix,
        limit=filters.limit,
        offset=filters.offset,
        scheduled=filters.scheduled,
        shared=True,
    )


async def get_stats(
    settings: Settings, window: str, process: str | None = None, *, scheduled: bool = True
) -> WorkflowStats:
    """The tiles' counts, narrowed as the list is."""
    return await asyncio.to_thread(
        workflows.stats,
        settings,
        window=window,
        process=process,
        scheduled=scheduled,
        shared=True,
    )


async def get_detail(settings: Settings, workflow_id: str) -> WorkflowDetail:
    """One workflow. Raises the service's `WorkflowNotFoundError`."""
    return await asyncio.to_thread(workflows.get, settings, workflow_id, shared=True)


async def act(
    settings: Settings,
    action: Action,
    workflow_id: str,
    *,
    start_step: int | None = None,
    to_version: str | None = None,
) -> Moved:
    """Cancel, resume, retry or fork one workflow; raises the service's errors."""
    calls: dict[str, Callable[[], workflows.Moved | None]] = {
        "cancel": lambda: workflows.cancel(settings, workflow_id, shared=True),
        "resume": lambda: workflows.resume(settings, workflow_id, shared=True),
        "retry": lambda: workflows.retry(settings, workflow_id, to_version=to_version, shared=True),
        "fork": lambda: workflows.fork(
            settings, workflow_id, start_step or 0, to_version=to_version, shared=True
        ),
    }
    moved = await asyncio.to_thread(calls[action])
    if moved is None:  # cancel
        return Moved(
            workflow_id=workflow_id, old_version=None, new_workflow_id=workflow_id, start_step=0
        )
    return Moved(**dataclasses.asdict(moved))


async def recover(
    settings: Settings, to_version: str, workflow_ids: list[str] | None
) -> list[Moved]:
    """Recover stranded workflows; raises `UnknownVersionError`."""
    moved = await asyncio.to_thread(
        workflows.recover, settings, to_version, ids=workflow_ids, shared=True
    )
    return [Moved(**dataclasses.asdict(m)) for m in moved]


async def trace_of(settings: Settings, workflow_id: str) -> TraceOut:
    """The trace of the workflow's root (MLflow keeps tags from root spans only)."""
    detail = await get_detail(settings, workflow_id)
    trace_id = await asyncio.to_thread(telemetry.find_trace, settings, detail.root_workflow_id)
    return TraceOut(trace_id=trace_id, url=telemetry.trace_page(settings, trace_id))


@router.get("")
async def get_workflows(
    request: Request,
    *,
    status: Annotated[list[Status] | None, Query()] = None,
    process: str | None = None,
    name: str | None = None,
    version: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    id_prefix: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    scheduled: bool = True,
) -> list[WorkflowSummary]:
    """Workflows, newest first.

    `status=stranded` lists the ones no running version finishes; `scheduled=false` leaves out
    the scheduled ticks (mail polls, deadline checks).
    """
    filters = Filters(
        status=list(status) if status else None,
        process=process,
        name=name,
        version=version,
        since=since,
        until=until,
        id_prefix=id_prefix,
        limit=limit,
        offset=offset,
        scheduled=scheduled,
    )
    return await list_rows(settings_of(request), filters)


@router.get("/stats")
async def get_workflow_stats(
    request: Request, window: Window = "24h", process: str | None = None, scheduled: bool = True
) -> WorkflowStats:
    """Counts by status and name over `window`, the stranded count, and queue depths."""
    return await get_stats(settings_of(request), window, process, scheduled=scheduled)


@router.post("/recover")
async def post_recover(request: Request, body: RecoverIn) -> list[Moved]:
    """Fork stranded workflows onto `to_version` (required) and cancel the originals."""
    with service_errors():
        return await recover(settings_of(request), body.to_version, body.workflow_ids)


@router.get("/{workflow_id:path}/trace")
async def get_trace(request: Request, workflow_id: str) -> TraceOut:
    """The MLflow trace of the workflow's root, or the experiment's trace list."""
    with service_errors():
        return await trace_of(settings_of(request), workflow_id)


@router.post("/{workflow_id:path}/cancel")
async def post_cancel(request: Request, workflow_id: str) -> Moved:
    """Cancel a running or queued workflow."""
    with service_errors():
        return await act(settings_of(request), "cancel", workflow_id)


@router.post("/{workflow_id:path}/resume")
async def post_resume(request: Request, workflow_id: str) -> Moved:
    """Continue a CANCELLED workflow of the running version under its own ID."""
    with service_errors():
        return await act(settings_of(request), "resume", workflow_id)


@router.post("/{workflow_id:path}/retry")
async def post_retry(request: Request, workflow_id: str, body: RetryIn) -> Moved:
    """Fork an ERROR workflow at its failed step (safe to repeat)."""
    with service_errors():
        return await act(settings_of(request), "retry", workflow_id, to_version=body.to_version)


@router.post("/{workflow_id:path}/fork")
async def post_fork(request: Request, workflow_id: str, body: ForkIn) -> Moved:
    """Run a finished workflow again from `start_step`; earlier steps are copied."""
    with service_errors():
        return await act(
            settings_of(request),
            "fork",
            workflow_id,
            start_step=body.start_step,
            to_version=body.to_version,
        )


@router.get("/{workflow_id:path}")
async def get_workflow(request: Request, workflow_id: str) -> WorkflowDetail:
    """One workflow: links, steps, outbox rows and, unless switched off, payloads."""
    with service_errors():
        return await get_detail(settings_of(request), workflow_id)
