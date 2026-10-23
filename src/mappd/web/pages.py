"""Core pages: the index of processes, the actions outbox and the Operations view."""

from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote, unquote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse
from markupsafe import escape
from pydantic import ValidationError

from mappd import actions, ops
from mappd.web import api, ops_api
from mappd.web.auth import principal
from mappd.web.http import OPERATOR
from mappd.web.templates import CORE
from mappd.web.workflows_api import settings_of

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mappd.ops import Cases

router = APIRouter(include_in_schema=False)
PAGE = 100
SUMMARY_KEYS = ("to", "subject", "type", "case_id", "chat_id", "text")


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Every registered process, with what waits for a person in it."""
    report = await ops.areport(ops_api.declared(request))
    context = {"ops": {p.process: p for p in report.processes}, "outbox": report.outbox}
    return CORE.TemplateResponse(request, "index.html", context)


def case_link(key: str, cases: Sequence[tuple[str, Cases]]) -> str | None:
    """The page of the case an action belongs to, read from its key; None when it names none.

    `mappd.send_email:<process>:<case>` (a reply), and `webhook:<process>:<case>:...` (an
    event, its `:` escaped by `ids.from_key`).
    """
    kind, _, rest = key.partition(":")
    process, _, case = (unquote(rest) if kind == "webhook" else rest).partition(":")
    if kind == "webhook":  # <case>:<event type>:...:<target>
        case = case.split(":", 1)[0]
    for name, declared in cases:
        if name == process and case:
            return declared.link.format(key=quote(case, safe=""))
    return None


def summary(payload: dict[str, Any]) -> str:
    """A payload in one short line: its telling fields (to, subject, ...), each cut short.

    A webhook's payload names env vars; the event it carries is what a person wants to see.
    """
    event = payload.get("event")
    if isinstance(event, dict):
        payload = event
    picked = [k for k in SUMMARY_KEYS if isinstance(payload.get(k), (str, int))]
    if not picked:
        picked = [k for k, v in payload.items() if isinstance(v, (str, int))][:2]
    parts = [f"{k}: {str(payload[k])[:60]}" for k in picked[:3]]
    return " · ".join(parts) or f"{len(payload)} fields"


def _view(request: Request, row: actions.ActionRow) -> dict[str, Any]:
    cases = getattr(request.app.state, "cases", [])
    link = case_link(row.idempotency_key, cases)
    return {"a": row, "case": link, "summary": summary(row.payload)}


@router.get("/actions", response_class=HTMLResponse)
async def actions_page(
    request: Request,
    status: api.Status = "unknown",
    limit: Annotated[int, Query(ge=1, le=1000)] = PAGE,
) -> HTMLResponse:
    """Actions of one status, newest change first; `unknown` rows can be settled here."""
    rows = await api.list_actions(status, limit=limit + 1)
    me = principal(request)
    context = {
        "views": [_view(request, r) for r in rows[:limit]],
        "status": status,
        "statuses": actions.STATUSES,
        "more": limit + PAGE if len(rows) > limit else None,
        "user": me.name if me else None,
    }
    return CORE.TemplateResponse(request, "actions.html", context)


@router.get("/ops", response_class=HTMLResponse)
async def ops_page(request: Request) -> HTMLResponse:
    """Cases waiting for a person, stuck cases and the outbox's trouble, per process."""
    report = await ops_api.full_report(request)
    return CORE.TemplateResponse(request, "ops.html", {"report": report})


@router.post("/actions/resolve", response_class=HTMLResponse)
async def resolve_row(
    request: Request,
    key: Annotated[str, Form()],
    took_effect: Annotated[str, Form()],
    operator: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Settle one row, retry its workflow, answer the row as it is now (200: htmx swaps it)."""
    me = principal(request)
    context: dict[str, Any] = {"error": None, "retried": None}
    try:
        name = me.name if me else OPERATOR.validate_python(operator)
        settled = await api.resolve(
            settings_of(request),
            key,
            api.Resolution(took_effect=took_effect == "true", operator=name),
        )
        context["retried"] = settled.retried
    except ValidationError:
        context["error"] = "Type your name above first: settling records who checked."
    except LookupError as e:
        context["error"] = str(e)
    found = await api.list_actions(None, key=key)
    if not found:
        message = escape(context["error"] or f"no action {key!r}")
        return HTMLResponse(f'<tr><td colspan="6" class="error">{message}</td></tr>')
    context.update(_view(request, found[0]))
    return CORE.TemplateResponse(request, "action_row.html", context)
