"""Durable workflows from outside the app: list, show, cancel, resume, retry, fork, recover.

The one service layer under `mappd workflow`, `/api/v1/workflows` and the `/workflows` pages
(AGENTS.md rule 7): every safety rule lives here, not in an adapter. Uses DBOSClient on the
system database, so these work whether the app runs or not.

A workflow is *stranded* when no running executor will finish it: PENDING or ENQUEUED on an
application version that no longer runs (a hard crash right before a deploy; the new version does
not recover another version's workflows), or PENDING under a `mappd process run` executor that
died. `recover` forks it onto the new version from the step after its last
recorded one (finished steps come from the journal) and cancels the original.
"""

import asyncio
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from dbos import DBOS, DBOSClient
from pydantic import BaseModel, Field

from mappd import actions, workflow_stats
from mappd.db import connect
from mappd.workflow_view import (
    CLI_EXECUTOR_PREFIX,
    FINISHED,
    IN_FLIGHT,
    ActionRef,
    WorkflowDetail,
    WorkflowStats,
    WorkflowSummary,
    allowed,
    duration,
    error_text,
    ms,
    payload,
    process_of,
    step_from,
    summary,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dbos._sys_db import WorkflowStatus

    from mappd.settings import Settings
    from mappd.workflow_view import Step

__all__ = ["CLI_EXECUTOR_PREFIX", "IN_FLIGHT"]

# A `mappd process run` is a short workflow; one still PENDING after this long lost its process.
CLI_ABANDONED_AFTER = 600.0
MAX_PARENT_HOPS = 20


class UnknownVersionError(ValueError):
    """The target application version has never run against this system database."""


class WorkflowNotFoundError(LookupError):
    """No workflow has this ID."""


class WorkflowStateError(ValueError):
    """The workflow's status does not allow the action."""


class WorkflowNotFailedError(WorkflowStateError):
    """Only ERROR workflows can be retried."""


class StepOutOfRangeError(ValueError):
    """A fork must start at a recorded step or right after the last one."""


@dataclass(frozen=True)
class Moved:
    """A stranded workflow forked onto another version."""

    workflow_id: str
    old_version: str | None
    new_workflow_id: str
    start_step: int


_SHARED: dict[tuple[str, str], DBOSClient] = {}
_SHARED_LOCK = threading.Lock()  # the app calls in from several worker threads at once


def _new_client(settings: Settings) -> DBOSClient:
    return DBOSClient(
        system_database_url=settings.system_database_url,
        application_name=settings.app_name,
        retry_connection_errors=False,  # a CLI and a healthcheck must fail, not hang
    )


@contextmanager
def client(settings: Settings, *, shared: bool = False) -> Iterator[DBOSClient]:
    """A DBOS client acting for this application on the system database.

    A fresh one is closed afterwards (the CLI). `shared=True` reuses one per database and
    application until `close_shared` (the app: no new connection pool per request).
    """
    if shared:
        key = (settings.system_database_url, settings.app_name)
        with _SHARED_LOCK:
            if key not in _SHARED:
                _SHARED[key] = _new_client(settings)
            shared_client = _SHARED[key]
        yield shared_client
        return
    dbos_client = _new_client(settings)
    try:
        yield dbos_client
    finally:
        dbos_client.destroy()


def close_shared() -> None:
    """Destroy the shared clients (the app's shutdown)."""
    with _SHARED_LOCK:
        while _SHARED:
            _SHARED.popitem()[1].destroy()


def registered_versions(settings: Settings, *, shared: bool = False) -> list[str]:
    """Application versions DBOS has seen for this application, newest first."""
    with client(settings, shared=shared) as c:
        return [v["version_name"] for v in c.list_application_versions()]


def _is_stranded(w: WorkflowStatus, current: str, abandoned_after: float) -> bool:
    if w.status in IN_FLIGHT and w.app_version != current:
        return True
    cli_run = (w.executor_id or "").startswith(CLI_EXECUTOR_PREFIX)
    age = time.time() - (w.created_at or 0) / 1000
    return w.status == "PENDING" and cli_run and age >= abandoned_after


def _summary(w: WorkflowStatus, current: str, abandoned_after: float) -> WorkflowSummary:
    error = error_text(w.error)
    return WorkflowSummary(
        workflow_id=w.workflow_id,
        name=w.name,
        process=process_of(w.name),
        status=str(w.status),
        stranded=_is_stranded(w, current, abandoned_after),
        app_version=w.app_version,
        queue=w.queue_name,
        created_at=ms(w.created_at),
        completed_at=ms(w.completed_at),
        duration_ms=duration(w.created_at, w.completed_at),
        parent_id=w.parent_workflow_id,
        forked_from=w.forked_from,
        error_summary=summary(error),
    )


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _names(
    settings: Settings, *, process: str | None, name: str | None = None, scheduled: bool = True
) -> list[str] | None:
    """The workflow names a filter keeps; None keeps all, [] keeps none."""
    if not process and not name and scheduled:
        return None
    kept = [name] if name else workflow_stats.names(settings)
    if process:
        kept = [n for n in kept if process_of(n) == process]
    if not scheduled:
        ticks = workflow_stats.scheduled_names(settings)
        kept = [n for n in kept if n not in ticks]
    return kept


def list_workflows(
    settings: Settings,
    *,
    status: list[str] | None = None,
    stranded: bool = False,
    limit: int = 100,
    ids: list[str] | None = None,
    abandoned_after: float = CLI_ABANDONED_AFTER,
    process: str | None = None,
    name: str | None = None,
    version: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    id_prefix: str | None = None,
    offset: int = 0,
    scheduled: bool = True,
    shared: bool = False,
) -> list[WorkflowSummary]:
    """This application's workflows, newest first.

    `stranded` keeps the ones no running executor will finish: PENDING or ENQUEUED on another
    version, or PENDING under a `mappd process run` executor older than `abandoned_after` seconds.
    `process` keeps workflows named "<process>.*"; `scheduled=False` leaves out the workflows
    that run on a schedule (most rows, and rarely the interesting ones); the other filters pass
    to DBOS.
    """
    names = _names(settings, process=process, name=name, scheduled=scheduled)
    if names == []:
        return []
    with client(settings, shared=shared) as c:
        rows = c.list_workflows(
            workflow_ids=ids,
            application_name=settings.app_name,
            status=list(IN_FLIGHT) if stranded else status,
            name=names,
            app_version=version,
            start_time=_iso(since),
            end_time=_iso(until),
            workflow_id_prefix=id_prefix,
            limit=None if stranded else limit,
            offset=None if stranded else offset or None,
            sort_desc=True,
            load_input=False,
            load_output=False,
        )
        # DBOS stores a workflow's error with its output: load it for the failed rows only.
        failed = [w.workflow_id for w in rows if str(w.status) == "ERROR"]
        errors = {
            w.workflow_id: w.error
            for w in (
                c.list_workflows(workflow_ids=failed, load_input=False, load_output=True)
                if failed
                else []
            )
        }
    for w in rows:
        if w.workflow_id in errors:
            w.error = errors[w.workflow_id]
    found = [_summary(w, settings.app_version, abandoned_after) for w in rows]
    if stranded:
        found = [w for w in found if w.stranded][offset : offset + limit]
    return found


def stats(
    settings: Settings,
    *,
    window: str = "24h",
    process: str | None = None,
    scheduled: bool = True,
    shared: bool = False,
) -> WorkflowStats:
    """Counts for the dashboard over `window` (a key of `workflow_stats.WINDOWS`).

    `process` and `scheduled` narrow the counts as they narrow the list.
    """
    del shared  # the counts read the system database directly
    since = datetime.now(UTC) - workflow_stats.WINDOWS[window]
    stranded = workflow_stats.stranded(
        settings, current=settings.app_version, abandoned_after=CLI_ABANDONED_AFTER
    )
    names = _names(settings, process=process, scheduled=scheduled)
    return workflow_stats.stats(settings, since=since, stranded=stranded, only=names)


def _status(c: DBOSClient, settings: Settings, workflow_id: str) -> WorkflowStatus:
    rows = c.list_workflows(
        workflow_ids=[workflow_id],
        application_name=settings.app_name,
        load_input=False,
        load_output=False,
    )
    if not rows:
        raise WorkflowNotFoundError(workflow_id)
    return rows[0]


def _last_step(c: DBOSClient, workflow_id: str) -> int:
    steps = c.list_workflow_steps(workflow_id, load_output=False)
    return max((s["function_id"] for s in steps), default=0)


def _root(c: DBOSClient, w: WorkflowStatus) -> str:
    root, parent = w.workflow_id, w.parent_workflow_id
    for _ in range(MAX_PARENT_HOPS):
        if not parent:
            break
        rows = c.list_workflows(workflow_ids=[parent], load_input=False, load_output=False)
        if not rows:
            break
        root, parent = rows[0].workflow_id, rows[0].parent_workflow_id
    return root


def _outbox(workflow_id: str, settings: Settings) -> list[ActionRef]:
    """The outbox rows the workflow claimed, in the order it claimed them."""
    with connect(settings) as conn:
        rows = actions.list_actions(conn, claimed_by=workflow_id, limit=500)
    return [
        ActionRef(
            idempotency_key=r.idempotency_key,
            kind=r.kind,
            status=r.status,
            per_workflow=r.idempotency_key.startswith(f"{workflow_id}:"),
        )
        for r in sorted(rows, key=lambda r: (r.created_at, r.idempotency_key))
    ]


def _pair(steps: list[Step], refs: list[ActionRef]) -> list[Step]:
    """Give each action step its own outbox row: the n-th step of a kind gets the n-th row."""
    queues: dict[str, list[ActionRef]] = {}
    for ref in refs:
        queues.setdefault(ref.kind, []).append(ref)
    paired = []
    for step in sorted(steps, key=lambda s: s.function_id):
        waiting = queues.get(step.name) if step.kind == "action" else None
        paired.append(step.model_copy(update={"outbox": waiting.pop(0)}) if waiting else step)
    return paired


def get(
    settings: Settings, workflow_id: str, *, payloads: bool | None = None, shared: bool = False
) -> WorkflowDetail:
    """One workflow with its links, steps and outbox rows; payloads unless switched off.

    `payloads=False` hides inputs and outputs even when the setting allows them; the setting
    off hides them whatever the caller asks.

    Raises:
        WorkflowNotFoundError: no workflow of this application has this ID.
    """
    show = settings.workflows_payloads and payloads is not False
    with client(settings, shared=shared) as c:
        rows = c.list_workflows(
            workflow_ids=[workflow_id],
            application_name=settings.app_name,
            load_input=show,
            load_output=True,  # errors live with the output; the output is dropped below
        )
        if not rows:
            raise WorkflowNotFoundError(workflow_id)
        w = rows[0]
        steps = c.list_workflow_steps(workflow_id, load_output=True)
        light = {"load_input": False, "load_output": False}
        children = c.list_workflows(parent_workflow_id=workflow_id, **light)  # type: ignore[arg-type]
        forks = c.list_workflows(forked_from=workflow_id, **light)  # type: ignore[arg-type]
        root = _root(c, w)
    base = _summary(w, settings.app_version, CLI_ABANDONED_AFTER)
    error = error_text(w.error)
    refs = _outbox(workflow_id, settings)
    return WorkflowDetail(
        **base.model_dump(),
        executor_id=w.executor_id,
        dequeued_at=ms(w.dequeued_at),
        recovery_attempts=w.recovery_attempts,
        children=[x.workflow_id for x in children],
        forks=[x.workflow_id for x in forks],
        root_workflow_id=root,
        input=payload(w.input) if show and w.input is not None else None,
        output=payload(w.output) if show and w.output is not None else None,
        error=error,
        steps=_pair([step_from(s, payloads=show) for s in steps], refs),
        actions=refs,
        payloads=show,
        allowed=allowed(
            str(w.status),
            stranded=base.stranded,
            current_version=w.app_version in (None, settings.app_version),
        ),
    )


def recover(
    settings: Settings,
    to_version: str,
    *,
    ids: list[str] | None = None,
    abandoned_after: float = CLI_ABANDONED_AFTER,
    shared: bool = False,
) -> list[Moved]:
    """Fork this application's stranded workflows onto `to_version`, then cancel the originals.

    The app running `to_version` executes the forks. Run this after the new version is up and
    the old one is gone: a version that still runs would race the fork. Safe to re-run: a
    workflow that already has a fork on `to_version` is only cancelled, not forked again.

    Raises:
        UnknownVersionError: DBOS has never seen `to_version` for this application; its forks
            would wait on a queue nothing serves.
    """
    _known(settings, to_version, shared=shared)
    moved: list[Moved] = []
    with client(settings, shared=shared) as c:
        rows = c.list_workflows(
            workflow_ids=ids,
            status=list(IN_FLIGHT),
            application_name=settings.app_name,
            load_input=False,
            load_output=False,
        )
        for w in rows:
            if not _is_stranded(w, to_version, abandoned_after):
                continue
            steps = c.list_workflow_steps(w.workflow_id, load_output=False)
            start = max((s["function_id"] for s in steps), default=0) + 1
            earlier = c.list_workflows(
                forked_from=w.workflow_id,
                app_version=to_version,
                load_input=False,
                load_output=False,
            )
            fork_id = (
                earlier[0].workflow_id
                if earlier
                else c.fork_workflow(
                    w.workflow_id, start, application_version=to_version
                ).workflow_id
            )
            c.cancel_workflow(w.workflow_id)
            moved.append(Moved(w.workflow_id, w.app_version, fork_id, start))
    return moved


def retry(
    settings: Settings, workflow_id: str, *, to_version: str | None = None, shared: bool = False
) -> Moved:
    """Fork an ERROR workflow at its failed step; the version `to_version` executes it.

    The fork replays the journal up to the first step that recorded an error and re-runs
    from there — the steps DBOS journaled do not repeat. Default target is the version the
    workflow ran on; pass `to_version` to retry on a newer deploy. Safe to re-run: a second
    call returns the existing fork (running or succeeded) instead of forking again; a fork
    that failed or was cancelled — a manual `fork` that went wrong — does not count. The
    ERROR original stays for the record — unlike `recover`, nothing is cancelled.

    Raises:
        WorkflowNotFoundError: no workflow has this ID.
        WorkflowNotFailedError: the workflow is not in ERROR (a PENDING one wants `recover`).
        UnknownVersionError: as in `recover`.
    """
    with client(settings, shared=shared) as c:
        w = _status(c, settings, workflow_id)
    if str(w.status) != "ERROR":
        raise WorkflowNotFailedError(
            f"only ERROR workflows can be retried; {workflow_id} is {w.status}"
        )
    target = _known(settings, to_version or w.app_version or settings.app_version, shared=shared)
    with client(settings, shared=shared) as c:
        steps = c.list_workflow_steps(workflow_id, load_output=True)
        errored = [s["function_id"] for s in steps if s.get("error")]
        start = min(errored, default=max((s["function_id"] for s in steps), default=0) + 1)
        earlier = c.list_workflows(
            forked_from=workflow_id,
            app_version=target,
            status=[*IN_FLIGHT, "SUCCESS"],
            load_input=False,
            load_output=False,
        )
        fork_id = (
            earlier[0].workflow_id
            if earlier
            else c.fork_workflow(workflow_id, start, application_version=target).workflow_id
        )
    return Moved(workflow_id, w.app_version, fork_id, start)


def _known(settings: Settings, version: str, *, shared: bool = False) -> str:
    known = registered_versions(settings, shared=shared)
    if version not in known:
        raise UnknownVersionError(
            f"version {version!r} has never run for {settings.app_name}; known: "
            + (", ".join(known) or "none")
        )
    return version


def cancel(settings: Settings, workflow_id: str, *, shared: bool = False) -> None:
    """Cancel one running or queued workflow.

    Raises:
        WorkflowNotFoundError: no workflow has this ID.
        WorkflowStateError: the workflow has already finished.
    """
    with client(settings, shared=shared) as c:
        w = _status(c, settings, workflow_id)
        if str(w.status) in FINISHED:
            raise WorkflowStateError(f"{workflow_id} has finished ({w.status}); nothing to cancel")
        c.cancel_workflow(workflow_id)


def resume(settings: Settings, workflow_id: str, *, shared: bool = False) -> Moved:
    """Continue a CANCELLED (or MAX_RECOVERY_ATTEMPTS_EXCEEDED) workflow under its own ID.

    DBOS runs it on its own version from the step after its last recorded one, so only a
    workflow of the running version can be resumed; an older one wants `fork --to-version`.

    Raises:
        WorkflowNotFoundError: no workflow has this ID.
        WorkflowStateError: the status does not allow it, or the workflow runs on another
            version.
    """
    with client(settings, shared=shared) as c:
        w = _status(c, settings, workflow_id)
        status = str(w.status)
        if status not in ("CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"):
            raise WorkflowStateError(
                f"only CANCELLED workflows can be resumed; {workflow_id} is {status}"
            )
        if w.app_version not in (None, settings.app_version):
            raise WorkflowStateError(
                f"{workflow_id} runs on {w.app_version}, not the running {settings.app_version};"
                " fork it with --to-version"
            )
        start = _last_step(c, workflow_id) + 1
        c.resume_workflow(workflow_id)
    return Moved(workflow_id, w.app_version, workflow_id, start)


def fork(
    settings: Settings,
    workflow_id: str,
    start_step: int,
    *,
    to_version: str | None = None,
    shared: bool = False,
) -> Moved:
    """Run a finished workflow again from `start_step` under a new ID; earlier steps are copied.

    Steps from `start_step` on run again: actions keyed by a business key find their outbox
    row done and do not act twice; actions keyed per workflow (AUTO) do. Only a finished
    workflow can be forked (a running one would run twice), and not while an earlier fork of
    it is still running (a double click must not fork twice).

    Raises:
        WorkflowNotFoundError: no workflow has this ID.
        WorkflowStateError: it is not finished, or a fork of it is still running.
        StepOutOfRangeError: `start_step` is not between 1 and the last step + 1.
        UnknownVersionError: as in `recover`.
    """
    with client(settings, shared=shared) as c:
        w = _status(c, settings, workflow_id)
        if str(w.status) not in FINISHED:
            raise WorkflowStateError(
                f"only finished workflows can be forked; {workflow_id} is {w.status}"
            )
        running = c.list_workflows(
            forked_from=workflow_id, status=list(IN_FLIGHT), load_input=False, load_output=False
        )
        if running:
            raise WorkflowStateError(
                f"fork {running[0].workflow_id} of {workflow_id} is still {running[0].status}"
            )
        last = _last_step(c, workflow_id)
    if not 1 <= start_step <= last + 1:
        raise StepOutOfRangeError(f"step {start_step} is not between 1 and {last + 1}")
    target = _known(settings, to_version or w.app_version or settings.app_version, shared=shared)
    with client(settings, shared=shared) as c:
        new = c.fork_workflow(workflow_id, start_step, application_version=target).workflow_id
    return Moved(workflow_id, w.app_version, new, start_step)


STOPPED = ("CANCELLED", "ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED")


class Processing(BaseModel):
    """A case's own workflow that stopped before the case moved on, and what to do."""

    workflow_id: str
    status: str
    remedy: str = Field(description="resume (after a cancel) or retry (after a failure)")


async def stopped(workflow_id: str) -> Processing | None:
    """The workflow, when it stopped without finishing (cancelled or failed); else None."""
    # The sync call on a worker thread: the async one binds DBOS's executor to the caller's
    # event loop, which a request's loop may close behind it.
    found = await asyncio.to_thread(DBOS.get_workflow_status, workflow_id)
    status = str(found.status) if found is not None else None
    if status not in STOPPED:
        return None
    remedy = "resume" if status == "CANCELLED" else "retry"
    return Processing(workflow_id=workflow_id, status=status, remedy=remedy)
