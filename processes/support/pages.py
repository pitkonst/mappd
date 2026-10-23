"""Operator pages of the support process; every handler calls service.py (AGENTS.md, rule 7).

Answers to the decision forms are 200 fragments in every case: htmx does not swap 4xx bodies.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from mappd.settings import Settings
from mappd.web import FormError, form, parse_form
from mappd.web.auth import principal as signed_in
from mappd.web.http import htmx_decided
from mappd.web.templates import templates_for

from . import domain, service, trust
from .schemas import Decision, Escalation, Resolution

router = APIRouter(include_in_schema=False)
templates = templates_for(Path(__file__).parent / "templates")

# "all": every status — what the dashboard links use, since a decision filter spans statuses
# (sent, shadowed, escalated...).
type Tab = Literal["awaiting_approval", "escalated", "resolved", "all"]
type DecisionFilter = Literal["auto", "edited", "unedited", "false_escalation"]


def _filters(status: str, category: str | None, decision: str | None) -> str:
    """The query string that keeps the queue's tab and filters."""
    params = {"status": status, "category": category, "decision": decision}
    return urlencode({k: v for k, v in params.items() if v})


def _hidden(request: Request) -> set[str]:
    """Signed-in: the middleware stamps `operator`, so it is a hidden, prefilled field."""
    return {"version", "operator"} if signed_in(request) else {"version"}


def _operator(request: Request) -> dict[str, str]:
    me = signed_in(request)
    return {"operator": me.name} if me else {}


@router.get("/support", response_class=HTMLResponse)
async def queue(
    request: Request,
    status: Tab = "awaiting_approval",
    category: str | None = None,
    decision: DecisionFilter | None = None,
) -> HTMLResponse:
    """The queue: awaiting approval, escalated or resolved, optionally filtered."""
    page = await service.list_tickets(
        status=None if status == "all" else status, category=category, decision=decision
    )
    context = {
        "tickets": page.items,
        "status": status,
        "query": _filters(status, category, decision),
        "extra": _filters("", category, decision),
    }
    return templates.TemplateResponse(request, "support/queue.html", context)


@router.get("/support/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, days: int = 30) -> HTMLResponse:
    """Can a category be trusted more than yesterday? The numbers and the verdict."""
    report = await trust.report(max(1, min(days, 365)))
    context = {"report": report, "mlflow": Settings.from_env().mlflow_url}
    return templates.TemplateResponse(request, "support/dashboard.html", context)


@router.get("/support/rows", response_class=HTMLResponse)
async def rows(
    request: Request,
    status: Tab = "awaiting_approval",
    category: str | None = None,
    decision: DecisionFilter | None = None,
) -> HTMLResponse:
    """The queue's rows alone; the queue re-fetches them on every support event."""
    page = await service.list_tickets(
        status=None if status == "all" else status, category=category, decision=decision
    )
    return templates.TemplateResponse(request, "support/rows.html", {"tickets": page.items})


@router.get("/support/tickets/{external_id:path}", response_class=HTMLResponse)
async def ticket(request: Request, external_id: str) -> HTMLResponse:
    """A ticket, its decision, and the decision forms that fit its status."""
    try:
        t = await service.get_ticket(external_id)
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e
    draft = t.output.draft if t.output else None
    topic = t.output.topic if t.output else None
    cited = await service.articles(draft.citations if draft else [])
    forms: dict[str, Any] = {}
    hidden, operator = _hidden(request), _operator(request)
    if t.status == "awaiting_approval":
        forms["approve"] = form(
            Decision,
            {
                "version": t.version,
                "reply": draft.reply if draft else "",
                "topic": topic,
                **operator,
            },
            hidden=hidden,
            id_prefix="approve",
        )
        forms["escalate"] = form(
            Escalation,
            {"version": t.version, "topic": topic, **operator},
            hidden=hidden,
            id_prefix="escalate",
        )
    elif t.status == "escalated":
        prior = t.decision.corrected.escalate_to if t.decision else None
        escalate_to = prior or (t.output.escalate_to if t.output else None)
        rule = domain.escalated_by_rule(t.output)
        forms["resolve"] = form(
            Resolution,
            {"version": t.version, "topic": topic, "escalate_to": escalate_to, **operator},
            hidden=hidden | ({"agent_could_handle", "outcome"} if rule else set()),
            id_prefix="resolve",
        )
    keys = {name: uuid4().hex for name in forms}
    rule = domain.escalated_by_rule(t.output)
    context = {"t": t, "articles": cited, "forms": forms, "keys": keys, "rule": rule}
    return templates.TemplateResponse(request, "support/ticket.html", context)


async def _decide[M: BaseModel](
    request: Request,
    external_id: str,
    model: type[M],
    call: Callable[[M], Awaitable[Any]],
    kind: str,
) -> HTMLResponse:
    """Parse a decision form, apply it through the service, answer the result fragment."""
    data = {k: v for k, v in (await request.form()).multi_items() if isinstance(v, str)}
    context: dict[str, object] = {"external_id": external_id, "kind": kind}
    try:
        decision = parse_form(model, data)
        result = await call(decision)
        context.update(state="repeated" if result.repeated else result.result, decision=decision)
    except FormError as e:
        context.update(
            state="invalid",
            form=form(model, data, errors=e.errors, hidden=_hidden(request), id_prefix=kind),
        )
    except service.HardRuleError as e:
        errors = {"agent_could_handle": str(e)}
        context.update(
            state="invalid",
            form=form(model, data, errors=errors, hidden=_hidden(request), id_prefix=kind),
        )
    except service.StaleVersionError as e:
        context.update(state="changed", current_version=e.current_version, status=e.status)
    except service.IdempotencyMismatchError:
        context["state"] = "mismatch"
    except service.SendFailedError as e:
        context.update(state="failed", workflow_id=e.workflow_id)
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e
    response = templates.TemplateResponse(request, "support/approval.html", context)
    return htmx_decided(response, str(context["state"]), kind)


@router.post("/support/tickets/{external_id:path}/approve", response_class=HTMLResponse)
async def approve(
    request: Request, external_id: str, idempotency_key: Annotated[str | None, Header()] = None
) -> HTMLResponse:
    """Send (or, in shadow mode, record) the reply."""
    return await _decide(
        request,
        external_id,
        Decision,
        lambda d: service.approve(external_id, d, idempotency_key=idempotency_key),
        "approve",
    )


@router.post("/support/tickets/{external_id:path}/escalate", response_class=HTMLResponse)
async def escalate(
    request: Request, external_id: str, idempotency_key: Annotated[str | None, Header()] = None
) -> HTMLResponse:
    """Hand the ticket to a team instead of sending the draft."""
    return await _decide(
        request,
        external_id,
        Escalation,
        lambda d: service.escalate(external_id, d, idempotency_key=idempotency_key),
        "escalate",
    )


@router.post("/support/tickets/{external_id:path}/resolve", response_class=HTMLResponse)
async def resolve(
    request: Request, external_id: str, idempotency_key: Annotated[str | None, Header()] = None
) -> HTMLResponse:
    """Close an escalated ticket and judge the escalation."""
    return await _decide(
        request,
        external_id,
        Resolution,
        lambda d: service.resolve(external_id, d, idempotency_key=idempotency_key),
        "resolve",
    )
