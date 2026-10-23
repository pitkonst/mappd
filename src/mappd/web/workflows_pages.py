"""Workflow pages: `/workflows` (counts and list) and `/workflows/{id}` (steps and actions).

Thin over the service calls in `mappd.web.workflows_api` (AGENTS.md rule 7). No browser
dialogs: an action button loads a confirmation fragment from the server, and its form posts
the action. Success answers `HX-Redirect` to the workflow now running; a refusal answers a
banner in a 200, so htmx swaps it in (the pattern of `/actions`).
"""

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from mappd import workflows
from mappd.web import workflows_api
from mappd.web.templates import CORE
from mappd.web.workflows_api import Filters, Window, service_errors, settings_of
from mappd.workflow_stats import WINDOWS
from mappd.workflow_view import FINISHED, IN_FLIGHT, WorkflowDetail

if TYPE_CHECKING:
    from mappd.settings import Settings
    from mappd.workflow_view import ActionRef, Step

router = APIRouter(include_in_schema=False)

PAGE_SIZE = 50
TILES = ("PENDING", "ENQUEUED", "ERROR", "stranded", "SUCCESS", "CANCELLED")
BADGES = {
    "SUCCESS": "ok",
    "ERROR": "bad",
    "MAX_RECOVERY_ATTEMPTS_EXCEEDED": "bad",
    "PENDING": "info",
    "ENQUEUED": "info",
    "stranded": "warn",
}
type Confirmable = Literal["cancel", "resume", "retry", "fork", "recover"]
type Postable = Literal["cancel", "resume", "retry", "fork"]
EFFECTS = {
    "cancel": "Stop this workflow. Steps already recorded stay; nothing after them runs.",
    "resume": "Continue this workflow under its own ID from the step after its last one.",
    "retry": "Fork this workflow at its failed step; the steps before it are copied.",
    "recover": "Fork this stranded workflow onto the chosen version and cancel it here.",
    "fork": "Run this workflow again from the chosen step under a new ID.",
}


def _statuses(raw: list[str] | None) -> list[str] | None:
    """The status filter from a form: empty choices dropped, unknown ones refused (422)."""
    chosen = [s for s in raw or [] if s]
    unknown = [s for s in chosen if s not in (*workflows_api.STATUSES, "stranded")]
    if unknown:
        raise HTTPException(422, f"unknown status {unknown[0]!r}")
    return chosen or None


def link(workflow_id: str) -> str:
    """The detail page of a workflow."""
    return f"/workflows/{quote(workflow_id, safe='')}"


def _bars(detail: WorkflowDetail) -> list[tuple[float, float] | None]:
    """Each step's (left, width) in percent of the steps' span; None without times."""
    times = [
        (s.started_at.timestamp(), s.completed_at.timestamp())
        for s in detail.steps
        if s.started_at and s.completed_at
    ]
    if not times:
        return [None for _ in detail.steps]
    first = min(t[0] for t in times)
    span = max(max(t[1] for t in times) - first, 1e-3)
    bars: list[tuple[float, float] | None] = []
    for s in detail.steps:
        if not (s.started_at and s.completed_at):
            bars.append(None)
            continue
        left = (s.started_at.timestamp() - first) / span * 100
        width = (s.completed_at.timestamp() - s.started_at.timestamp()) / span * 100
        bars.append((round(min(left, 99.5), 2), round(max(width, 0.5), 2)))
    return bars


def _action_effect(ref: ActionRef | None) -> str:
    if ref is None:
        return "will send (no outbox record)"
    if ref.per_workflow:
        return "will send again (key per workflow)"
    if ref.status == "unknown":
        return "stops: outcome unknown, settle it in /actions first"
    if ref.status == "done":
        return "will not resend (outbox: done)"
    return f"will send again (outbox: {ref.status})"


def effect(step: Step) -> str:
    """What happens to one step when a fork runs it again (`mappd.actions` outbox rules)."""
    if step.kind == "action":
        return _action_effect(step.outbox)
    if step.kind == "transaction":
        return "guarded: a replay matches no row"
    return "runs again"


async def _versions(settings: Settings) -> list[str]:
    known = await asyncio.to_thread(workflows.registered_versions, settings, shared=True)
    return [settings.app_version, *(v for v in known if v != settings.app_version)]


def _detail_context(request: Request, detail: WorkflowDetail) -> dict[str, Any]:
    return {
        "w": detail,
        "bars": _bars(detail),
        "live": detail.status in IN_FLIGHT,
        "link": link,
        "badges": BADGES,
        "finished": detail.status in FINISHED,
    }


def _banner(request: Request, message: str) -> HTMLResponse:
    return CORE.TemplateResponse(request, "workflow_banner.html", {"message": message})


def _redirect(workflow_id: str) -> Response:
    return Response(status_code=200, headers={"HX-Redirect": link(workflow_id)})


async def _list_context(
    request: Request,
    *,
    status: list[str] | None,
    process: str | None,
    version: str | None,
    id_prefix: str | None,
    window: str,
    offset: int,
    live: bool,
    scheduled: bool,
) -> dict[str, Any]:
    settings = settings_of(request)
    filters = Filters(
        scheduled=scheduled,
        status=status,
        since=datetime.now(UTC) - WINDOWS[window],
        process=process or None,
        version=version or None,
        id_prefix=id_prefix or None,
        limit=PAGE_SIZE,
        offset=offset,
    )
    rows, stats, versions = await asyncio.gather(
        workflows_api.list_rows(settings, filters),
        workflows_api.get_stats(settings, window, process or None, scheduled=scheduled),
        _versions(settings),
    )
    query = {
        k: v
        for k, v in {
            "status": status or [],
            "process": process or "",
            "version": version or "",
            "id_prefix": id_prefix or "",
            "window": window,
            "offset": offset or "",
            "scheduled": "1" if scheduled else "",
        }.items()
        if v
    }
    counts = {**stats.by_status, "stranded": stats.stranded}
    narrowing = {
        "process": process,
        "version": version,
        "id_prefix": id_prefix,
        "scheduled": "1" if scheduled else None,
    }
    kept = {k: v for k, v in narrowing.items() if v}

    def tile_query(tile: str) -> str:
        return urlencode({"status": tile, "window": window, **kept})

    def window_query(other: str) -> str:
        return urlencode({"status": status or [], "window": other, **kept}, doseq=True)

    return {
        "tile_query": tile_query,
        "window_query": window_query,
        "rows": rows,
        "stats": stats,
        "counts": counts,
        "tiles": [*TILES, *(s for s in counts if s not in TILES)],
        "badges": BADGES,
        "versions": versions,
        "filters": {"status": status or [], "process": process, "version": version},
        "id_prefix": id_prefix or "",
        "window": window,
        "windows": ("1h", "24h", "7d"),
        "statuses": workflows_api.STATUSES,
        "live": live,
        "scheduled": scheduled,
        "scheduled_toggle": urlencode(
            {k: v for k, v in {**query, "scheduled": "" if scheduled else "1"}.items() if v},
            doseq=True,
        ),
        "query": urlencode(query, doseq=True),
        "paused_query": urlencode({**query, "live": "0"}, doseq=True),
        "newer": urlencode({**query, "offset": max(offset - PAGE_SIZE, 0)}, doseq=True)
        if offset
        else None,
        "older": urlencode({**query, "offset": offset + PAGE_SIZE}, doseq=True)
        if len(rows) == PAGE_SIZE
        else None,
        "link": link,
    }


@router.get("/workflows", response_class=HTMLResponse)
async def workflows_page(
    request: Request,
    *,
    status: Annotated[list[str] | None, Query()] = None,
    process: str | None = None,
    version: str | None = None,
    id_prefix: str | None = None,
    window: Window = "24h",
    offset: Annotated[int, Query(ge=0)] = 0,
    live: bool = True,
    scheduled: bool = False,
) -> HTMLResponse:
    """Counts, queues and the filtered list; scheduled ticks only with `scheduled=1`."""
    context = await _list_context(
        request,
        status=_statuses(status),
        process=process,
        version=version,
        id_prefix=id_prefix,
        window=window,
        offset=offset,
        live=live,
        scheduled=scheduled,
    )
    return CORE.TemplateResponse(request, "workflows.html", context)


@router.get("/workflows/list", response_class=HTMLResponse)
async def workflows_list(
    request: Request,
    *,
    status: Annotated[list[str] | None, Query()] = None,
    process: str | None = None,
    version: str | None = None,
    id_prefix: str | None = None,
    window: Window = "24h",
    offset: Annotated[int, Query(ge=0)] = 0,
    scheduled: bool = False,
) -> HTMLResponse:
    """The tiles and the table alone: what the live list refreshes."""
    context = await _list_context(
        request,
        status=_statuses(status),
        process=process,
        version=version,
        id_prefix=id_prefix,
        window=window,
        offset=offset,
        live=True,
        scheduled=scheduled,
    )
    return CORE.TemplateResponse(request, "workflow_list.html", context)


@router.post("/workflows/recover", response_class=HTMLResponse)
async def recover(
    request: Request,
    to_version: Annotated[str, Form()],
    workflow_id: Annotated[str | None, Form()] = None,
) -> Response:
    """Recover one stranded workflow (`workflow_id`) or all of them onto `to_version`."""
    try:
        moved = await workflows_api.recover(
            settings_of(request), to_version, [workflow_id] if workflow_id else None
        )
    except workflows.UnknownVersionError as e:
        return _banner(request, str(e))
    if len(moved) == 1:
        return _redirect(moved[0].new_workflow_id)
    if not moved:
        return _banner(request, "nothing to recover")
    return Response(status_code=200, headers={"HX-Redirect": "/workflows?status=stranded"})


@router.get("/workflows/{workflow_id:path}/trace")
async def trace(request: Request, workflow_id: str) -> RedirectResponse:
    """MLflow's page for the workflow's trace (or its experiment's trace list)."""
    with service_errors():
        found = await workflows_api.trace_of(settings_of(request), workflow_id)
    return RedirectResponse(found.url, status_code=303)


@router.get("/workflows/{workflow_id:path}/confirm/{action}", response_class=HTMLResponse)
async def confirm(
    request: Request, workflow_id: str, action: Confirmable, step: int | None = None
) -> HTMLResponse:
    """What the action will do, and the form that does it."""
    settings = settings_of(request)
    try:
        detail = await workflows_api.get_detail(settings, workflow_id)
    except workflows.WorkflowNotFoundError:
        return _banner(request, f"no workflow {workflow_id!r}")
    start = step or 1
    effects = [(s, effect(s)) for s in detail.steps if s.function_id >= start]
    context = {
        "w": detail,
        "action": action,
        "effect": EFFECTS[action],
        "step": start,
        "effects": effects,
        "versions": await _versions(settings),
        "link": link,
    }
    return CORE.TemplateResponse(request, "workflow_confirm.html", context)


@router.post("/workflows/{workflow_id:path}/{action}", response_class=HTMLResponse)
async def act(
    request: Request,
    workflow_id: str,
    action: Postable,
    start_step: Annotated[int | None, Form()] = None,
    to_version: Annotated[str | None, Form()] = None,
) -> Response:
    """Do the action; go to the workflow now running it, or show why not."""
    try:
        moved = await workflows_api.act(
            settings_of(request),
            action,
            workflow_id,
            start_step=start_step,
            to_version=to_version or None,
        )
    except workflows.WorkflowNotFoundError:
        return _banner(request, f"no workflow {workflow_id!r}")
    except (
        workflows.WorkflowStateError,
        workflows.StepOutOfRangeError,
        workflows.UnknownVersionError,
    ) as e:
        return _banner(request, str(e))
    return _redirect(moved.new_workflow_id)


@router.get("/workflows/{workflow_id:path}/live", response_class=HTMLResponse)
async def live(request: Request, workflow_id: str) -> HTMLResponse:
    """The header and steps alone: what an unfinished workflow's page refreshes."""
    with service_errors():
        detail = await workflows_api.get_detail(settings_of(request), workflow_id)
    return CORE.TemplateResponse(request, "workflow_live.html", _detail_context(request, detail))


@router.get("/workflows/{workflow_id:path}", response_class=HTMLResponse)
async def workflow_page(request: Request, workflow_id: str) -> HTMLResponse:
    """One workflow: header, actions, steps on a timeline, payloads."""
    try:
        detail = await workflows_api.get_detail(settings_of(request), workflow_id)
    except workflows.WorkflowNotFoundError as e:
        raise HTTPException(404, f"no workflow {workflow_id!r}") from e
    return CORE.TemplateResponse(request, "workflow.html", _detail_context(request, detail))
