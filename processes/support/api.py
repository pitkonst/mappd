"""HTTP API of the support process; every handler calls service.py (AGENTS.md, rule 7)."""

from collections.abc import AsyncIterable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.sse import EventSourceResponse, ServerSentEvent

from mappd.events import BROKER
from mappd.web.http import Conflict, conflict, idempotency_key

from . import service, trust
from .schemas import (
    ApprovalResult,
    Decision,
    DecisionResult,
    Escalation,
    Resolution,
    SchemaOut,
    StatusFilter,
    SupportOutput,
    TicketAccepted,
    TicketIn,
    TicketOut,
    TicketPage,
    TrustReport,
)

router = APIRouter(prefix="/api/v1/support", tags=["support"])


@router.post("/tickets", status_code=202)
async def create_ticket(ticket: TicketIn) -> TicketAccepted:
    """Accept a ticket."""
    workflow_id = await service.ingest(ticket)
    return TicketAccepted(external_id=ticket.external_id, workflow_id=workflow_id)


@router.get("/tickets")
async def list_tickets(
    status: Annotated[StatusFilter | None, Query(description="a status, or `all`")] = None,
    category: str | None = None,
    decision: Literal["auto", "edited", "unedited", "false_escalation"] | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: str | None = None,
) -> TicketPage:
    """Tickets newest first, filtered by status, category and decision; `next` pages on."""
    try:
        return await service.list_tickets(
            status=status, category=category, decision=decision, limit=limit, before=before
        )
    except service.BadCursorError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/tickets/{external_id:path}")
async def get_ticket(external_id: str) -> TicketOut:
    """The ticket with its status, output and version."""
    try:
        return await service.get_ticket(external_id)
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e


@router.post(
    "/tickets/{external_id:path}/approve",
    responses={409: {"model": Conflict}, 502: {"description": "the reply is not confirmed sent"}},
)
async def approve(
    external_id: str,
    decision: Decision,
    key: Annotated[str | None, Depends(idempotency_key)],
) -> ApprovalResult:
    """Approve the draft at `decision.version`; `Idempotency-Key` makes retries safe."""
    try:
        return await service.approve(external_id, decision, idempotency_key=key)
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except service.IdempotencyMismatchError as e:
        raise HTTPException(422, str(e)) from e
    except service.StaleVersionError as e:
        conflict("ticket changed or already decided", e.current_version, e.status)
    except service.SendFailedError as e:
        raise HTTPException(502, str(e)) from e


@router.post("/tickets/{external_id:path}/escalate", responses={409: {"model": Conflict}})
async def escalate(
    external_id: str,
    decision: Escalation,
    key: Annotated[str | None, Depends(idempotency_key)],
) -> DecisionResult:
    """Hand an awaiting ticket to a team instead of sending the draft; nothing is sent."""
    try:
        return await service.escalate(external_id, decision, idempotency_key=key)
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except service.IdempotencyMismatchError as e:
        raise HTTPException(422, str(e)) from e
    except service.StaleVersionError as e:
        conflict("ticket changed or already decided", e.current_version, e.status)
    except service.SendFailedError as e:
        raise HTTPException(502, str(e)) from e


@router.post("/tickets/{external_id:path}/resolve", responses={409: {"model": Conflict}})
async def resolve(
    external_id: str,
    decision: Resolution,
    key: Annotated[str | None, Depends(idempotency_key)],
) -> DecisionResult:
    """Close an escalated ticket; mark it when the agent could have handled it itself."""
    try:
        return await service.resolve(external_id, decision, idempotency_key=key)
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except (service.IdempotencyMismatchError, service.HardRuleError) as e:
        raise HTTPException(422, str(e)) from e
    except service.StaleVersionError as e:
        conflict("ticket changed or already decided", e.current_version, e.status)
    except service.SendFailedError as e:
        raise HTTPException(502, str(e)) from e


@router.get("/trust")
async def trust_report(days: Annotated[int, Query(ge=1, le=365)] = 30) -> TrustReport:
    """Per category: approved without edits next to false escalations, and a readiness verdict."""
    return await trust.report(days)


@router.get("/schema")
async def schema() -> SchemaOut:
    """JSON Schemas of the decision output and of an approval, for an own front end."""
    return SchemaOut(
        output=SupportOutput.model_json_schema(), decision=Decision.model_json_schema()
    )


@router.get("/events", response_class=EventSourceResponse)
async def stream() -> AsyncIterable[ServerSentEvent]:
    """Support events as they happen (this node only, no replay: refetch on reconnect)."""
    async for e in BROKER.subscribe("support"):
        yield ServerSentEvent(data=e.model_dump(mode="json"), id=e.id)
