"""Core API: the actions outbox, for operators and integrators (`/api/v1/actions`)."""

import asyncio
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from mappd import actions, workflows
from mappd.db import connect
from mappd.web.auth import principal
from mappd.web.http import Operator
from mappd.web.workflows_api import settings_of

if TYPE_CHECKING:
    from mappd.settings import Settings

router = APIRouter(prefix="/api/v1/actions", tags=["actions"])

type Status = Literal["pending", "sending", "done", "failed", "unknown"]


class Resolution(BaseModel):
    """A person's finding after checking with the receiver; the operator is recorded."""

    took_effect: bool
    operator: Operator
    result: dict[str, Any] | None = None


class Settled(BaseModel):
    """What settling did: the row as it is now, and the workflow retry it started, if any."""

    action: actions.ActionRow
    retried: str | None = Field(
        description="the fork that finishes the case (the action's workflow had failed)"
    )


def _list(
    status: str | None, kind: str | None, key: str | None, limit: int
) -> list[actions.ActionRow]:
    with connect() as conn:
        return actions.list_actions(conn, status=status, kind=kind, key=key, limit=limit)


def _resolve(key: str, resolution: Resolution) -> actions.ActionRow:
    with connect() as conn:
        actions.resolve(
            conn,
            key,
            took_effect=resolution.took_effect,
            operator=resolution.operator,
            result=resolution.result,
        )
        [row] = actions.list_actions(conn, key=key)
    return row


def _retry(settings: Settings, workflow_id: str | None) -> str | None:
    """Retry the action's workflow when it ended in ERROR: that finishes its case.

    Took effect: the retry replays the action from the outbox and nothing is sent again. Not
    sent: the retry sends it. A workflow still running or recovering is left alone.
    """
    if workflow_id is None:
        return None
    try:
        return workflows.retry(settings, workflow_id, shared=True).new_workflow_id
    except workflows.WorkflowNotFailedError, workflows.WorkflowNotFoundError:
        return None


async def list_actions(
    status: str | None, kind: str | None = None, key: str | None = None, limit: int = 100
) -> list[actions.ActionRow]:
    """The service call the API and the page share (the CLI calls `mappd.actions` directly)."""
    return await asyncio.to_thread(_list, status, kind, key, limit)


async def resolve(settings: Settings, key: str, resolution: Resolution) -> Settled:
    """Settle an unknown action, then retry its failed workflow so the case finishes.

    Raises:
        LookupError: no unknown action has `key`.
    """
    row = await asyncio.to_thread(_resolve, key, resolution)
    retried = await asyncio.to_thread(_retry, settings, row.claimed_by)
    return Settled(action=row, retried=retried)


@router.get("")
async def get_actions(
    status: Status | None = None,
    kind: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[actions.ActionRow]:
    """Actions, newest change first."""
    return await list_actions(status, kind, limit=limit)


@router.post("/{key:path}/resolve")
async def post_resolve(request: Request, key: str, resolution: Resolution) -> Settled:
    """Settle an `unknown` action and retry its failed workflow, which finishes the case.

    Took effect: done with `result`. Not sent: pending, so the retry sends it. Signed in, the
    operator is the signed-in user.
    """
    me = principal(request)
    if me is not None:
        resolution = resolution.model_copy(update={"operator": me.name})
    try:
        return await resolve(settings_of(request), key, resolution)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
