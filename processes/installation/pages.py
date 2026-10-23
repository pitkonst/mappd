"""The dispatcher's pages: the jobs list and one job's timeline with its actions.

Every action posts to a page route that calls the same service as `/api/v1/installation`
(AGENTS.md, rule 7) and answers the re-rendered job panel; the page refreshes the panel on the
process's events. With sign-in on the dispatcher field shows the signed-in
name, read-only; otherwise the dispatcher types their name.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from mappd.web.auth import principal as signed_in
from mappd.web.http import OPERATOR
from mappd.web.templates import templates_for

from . import service
from .schemas import JobOut

router = APIRouter(include_in_schema=False)
templates = templates_for(Path(__file__).parent / "templates")


type Tab = Literal["attention", "open", "handed_over", "all"]
TABS: tuple[tuple[str, str], ...] = (
    ("attention", "needs you"),
    ("open", "open"),
    ("handed_over", "handed over"),
    ("all", "all"),
)


def needs_dispatcher(job: JobOut) -> bool:
    """Blocked, a proposal to decide, or a deadline passed: a dispatcher should look.

    A handed-over job never does: it is finished, whatever it left behind.
    """
    if job.state == "handed_over":
        return False
    pending = any(p.status == "pending" for p in job.proposals)
    return job.state == "blocked" or pending or bool(job.overdue)


def in_tab(job: JobOut, tab: str) -> bool:
    """Whether the jobs list shows `job` under `tab`."""
    if tab == "attention":
        return needs_dispatcher(job)
    if tab == "open":
        return job.state != "handed_over"
    if tab == "handed_over":
        return job.state == "handed_over"
    return True


@router.get("/installation", response_class=HTMLResponse)
async def jobs(request: Request, tab: Tab = "attention") -> HTMLResponse:
    """Jobs by tab: what needs a dispatcher (the default), open, handed over, all."""
    rows = await service.list_jobs()
    context = {
        "jobs": [j for j in rows if in_tab(j, tab)],
        "tab": tab,
        "tabs": TABS,
        "counts": {t: sum(in_tab(j, t) for j in rows) for t, _ in TABS},
    }
    return templates.TemplateResponse(request, "installation/jobs.html", context)


async def _context(request: Request, external_id: str, error: str | None = None) -> dict[str, Any]:
    try:
        job = await service.get_job(external_id)
        entries = await service.timeline(external_id)
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e
    me = signed_in(request)
    return {
        "job": job,
        "entries": entries,
        "error": error,
        "send_key": uuid4().hex,
        "user": me.name if me else None,
    }


async def _panel(
    request: Request, external_id: str, *, error: str | None = None, status: int = 200
) -> HTMLResponse:
    context = await _context(request, external_id, error)
    return templates.TemplateResponse(
        request, "installation/panel.html", context, status_code=status
    )


@router.get("/installation/jobs/{external_id}", response_class=HTMLResponse)
async def job(request: Request, external_id: str) -> HTMLResponse:
    """One job: its timeline, and what a dispatcher can do now."""
    context = await _context(request, external_id)
    return templates.TemplateResponse(request, "installation/job.html", context)


@router.get("/installation/jobs/{external_id}/live", response_class=HTMLResponse)
async def live(request: Request, external_id: str) -> HTMLResponse:
    """The job's state and timeline, without the forms.

    The page refreshes this on events, so nothing a dispatcher is typing is ever replaced.
    """
    context = await _context(request, external_id)
    return templates.TemplateResponse(request, "installation/live.html", context)


async def _act(
    request: Request, external_id: str, act: Callable[[dict[str, str]], Awaitable[Any]]
) -> HTMLResponse:
    form = {k: v for k, v in (await request.form()).multi_items() if isinstance(v, str)}
    try:
        await act(form)
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except (service.StaleVersionError, service.NotHolderError) as e:
        return await _panel(request, external_id, error=str(e), status=409)
    except service.UpstreamError as e:
        return await _panel(request, external_id, error=str(e), status=502)
    except (KeyError, ValueError) as e:
        return await _panel(request, external_id, error=f"missing or bad field: {e}", status=422)
    return await _panel(request, external_id)


def _version(form: dict[str, str]) -> int:
    return int(form["version"])


def _operator(form: dict[str, str]) -> str:
    """The dispatcher's name, checked as the API checks it: never blank."""
    try:
        return OPERATOR.validate_python(form.get("operator", ""))
    except ValidationError as e:
        raise ValueError("dispatcher: enter your name") from e


@router.post("/installation/jobs/{external_id}/takeover", response_class=HTMLResponse)
async def take_over(request: Request, external_id: str) -> HTMLResponse:
    """Hold the conversation."""
    return await _act(
        request,
        external_id,
        lambda f: service.take_over(external_id, _operator(f), _version(f)),
    )


@router.post("/installation/jobs/{external_id}/release", response_class=HTMLResponse)
async def release(request: Request, external_id: str) -> HTMLResponse:
    """Hand the conversation back to the agent."""
    return await _act(
        request, external_id, lambda f: service.release(external_id, _operator(f), _version(f))
    )


@router.post("/installation/jobs/{external_id}/advance", response_class=HTMLResponse)
async def advance(request: Request, external_id: str) -> HTMLResponse:
    """Move the job one step by hand."""
    return await _act(
        request, external_id, lambda f: service.advance(external_id, _operator(f), _version(f))
    )


@router.post("/installation/jobs/{external_id}/unblock", response_class=HTMLResponse)
async def unblock(request: Request, external_id: str) -> HTMLResponse:
    """Clear the block."""
    return await _act(
        request, external_id, lambda f: service.unblock(external_id, _version(f), _operator(f))
    )


@router.post("/installation/jobs/{external_id}/messages", response_class=HTMLResponse)
async def send(request: Request, external_id: str) -> HTMLResponse:
    """The holder's message to the installer (the key comes with the rendered form)."""
    return await _act(
        request,
        external_id,
        lambda f: service.send(external_id, _operator(f), f["text"], f["key"]),
    )


@router.post(
    "/installation/jobs/{external_id}/proposals/{proposal_id}/confirm",
    response_class=HTMLResponse,
)
async def confirm(request: Request, external_id: str, proposal_id: str) -> HTMLResponse:
    """Run a proposal's commitment."""
    return await _act(
        request, external_id, lambda f: service.confirm(external_id, proposal_id, _operator(f))
    )


@router.post(
    "/installation/jobs/{external_id}/proposals/{proposal_id}/reject",
    response_class=HTMLResponse,
)
async def reject(request: Request, external_id: str, proposal_id: str) -> HTMLResponse:
    """Turn a proposal down."""
    return await _act(
        request,
        external_id,
        lambda f: service.reject(external_id, proposal_id, _operator(f), f.get("note", "")),
    )
