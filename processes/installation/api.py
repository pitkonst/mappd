"""HTTP API of the installation process: Telegram's webhook and the dispatcher's routes."""

from collections.abc import AsyncIterable
from typing import Annotated, Any, NoReturn

import httpx2
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field

from mappd.channels.telegram import TelegramError, verify_secret
from mappd.events import BROKER, Event
from mappd.web.http import Operator, idempotency_key

from . import intake, service, telegram
from .schemas import JobIn, JobOut, ProposalOut, TimelineEntry

router = APIRouter(prefix="/api/v1/installation", tags=["installation"])


@router.post("/inbound/telegram")
async def telegram_webhook(
    request: Request,
    update: dict[str, Any] = Body(...),  # noqa: B008 - FastAPI idiom
) -> dict[str, bool]:
    """Telegram's webhook: only with the secret set at setWebhook; a redelivery runs once."""
    if not verify_secret(dict(request.headers), telegram.secret()):
        raise HTTPException(401, "missing or wrong secret token")
    await intake.receive(update)
    return {"ok": True}


class Unblock(BaseModel):
    """A dispatcher's decision to clear a block, at the version they saw."""

    version: int = Field(ge=0)
    operator: Operator


@router.post("/jobs", status_code=201)
async def create_job(job: JobIn) -> JobOut:
    """A new job (and installer); answers the deep link while the installer is unlinked."""
    try:
        return await service.create_job(job)
    except (service.DuplicateJobError, service.OpenJobError) as e:
        raise HTTPException(409, str(e)) from e


@router.get("/jobs")
async def list_jobs() -> list[JobOut]:
    """Every job, newest first."""
    return await service.list_jobs()


@router.get("/jobs/{external_id}")
async def get_job(external_id: str) -> JobOut:
    """One job with its conversation."""
    try:
        return await service.get_job(external_id)
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e


class Decision(BaseModel):
    """A dispatcher's decision on a proposal."""

    operator: Operator
    note: str = ""


@router.post("/jobs/{external_id}/proposals/{proposal_id}/confirm")
async def confirm(external_id: str, proposal_id: str, decision: Decision) -> ProposalOut:
    """Run a proposal's commitment (once) and tell the installer; 409 when it was rejected."""
    try:
        return await service.confirm(external_id, proposal_id, decision.operator)
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except service.StaleVersionError as e:
        raise HTTPException(409, str(e)) from e
    except service.UpstreamError as e:
        raise HTTPException(502, str(e)) from e


@router.post("/jobs/{external_id}/proposals/{proposal_id}/reject")
async def reject(external_id: str, proposal_id: str, decision: Decision) -> ProposalOut:
    """Turn a pending proposal down; 409 when it was already decided."""
    try:
        return await service.reject(external_id, proposal_id, decision.operator, decision.note)
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except service.StaleVersionError as e:
        raise HTTPException(409, str(e)) from e


class Hold(BaseModel):
    """A dispatcher acting on a job at the version they saw."""

    operator: Operator
    version: int = Field(ge=0)


class Say(BaseModel):
    """The holder's message to the installer."""

    operator: Operator
    text: str = Field(min_length=1, max_length=4000)


def _decided(e: Exception) -> NoReturn:
    if isinstance(e, service.NotFoundError):
        raise HTTPException(404, str(e)) from e
    raise HTTPException(409, str(e)) from e


@router.post("/jobs/{external_id}/takeover")
async def take_over(external_id: str, hold: Hold) -> JobOut:
    """Hold the conversation; the agent stays silent until release. 409 on a stale version."""
    try:
        return await service.take_over(external_id, hold.operator, hold.version)
    except (service.NotFoundError, service.StaleVersionError) as e:
        _decided(e)


@router.post("/jobs/{external_id}/release")
async def release(external_id: str, hold: Hold) -> JobOut:
    """Hand the conversation back to the agent; 409 unless the holder, at the current version."""
    try:
        return await service.release(external_id, hold.operator, hold.version)
    except (service.NotFoundError, service.NotHolderError, service.StaleVersionError) as e:
        _decided(e)


@router.post("/jobs/{external_id}/messages")
async def send(
    external_id: str, say: Say, key: Annotated[str | None, Depends(idempotency_key)]
) -> JobOut:
    """The holder's message to the installer; `Idempotency-Key` is required, 409 for others."""
    if key is None:
        raise HTTPException(422, "Idempotency-Key is required")
    try:
        return await service.send(external_id, say.operator, say.text, key)
    except (service.NotFoundError, service.NotHolderError) as e:
        _decided(e)


@router.post("/jobs/{external_id}/advance")
async def advance(external_id: str, hold: Hold) -> JobOut:
    """Move the job one step by hand; 409 on a stale version or a job that cannot move."""
    try:
        return await service.advance(external_id, hold.operator, hold.version)
    except (service.NotFoundError, service.StaleVersionError) as e:
        _decided(e)


@router.get("/jobs/{external_id}/timeline")
async def timeline(external_id: str) -> list[TimelineEntry]:
    """The job's messages and events in the order they happened."""
    try:
        return await service.timeline(external_id)
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e


@router.get(
    "/jobs/{external_id}/photos/{file_id}",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def photo(external_id: str, file_id: str) -> Response:
    """A photo the installer sent on this job (only this job's photos)."""
    try:
        return Response(await service.photo(external_id, file_id), media_type="image/jpeg")
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except (TelegramError, httpx2.HTTPError) as e:
        raise HTTPException(502, f"Telegram could not serve the photo: {e}") from e


def wanted(event: Event, job: str | None) -> bool:
    """Whether a subscriber to `job` (None: every job) gets this event."""
    return job is None or event.case_id == job


@router.get("/events", response_class=EventSourceResponse)
async def stream(job: str | None = None) -> AsyncIterable[ServerSentEvent]:
    """Installation events as they happen; `?job=` keeps one job's.

    This node only, no replay: refetch on reconnect.
    """
    async for e in BROKER.subscribe("installation"):
        if wanted(e, job):
            yield ServerSentEvent(data=e.model_dump(mode="json"), id=e.id)


@router.post("/jobs/{external_id}/unblock")
async def unblock(external_id: str, decision: Unblock) -> JobOut:
    """Clear a block at `version`; 409 when the job moved on or is not blocked."""
    try:
        return await service.unblock(external_id, decision.version, decision.operator)
    except service.NotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except service.StaleVersionError as e:
        raise HTTPException(409, str(e)) from e
