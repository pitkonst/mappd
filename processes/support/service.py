"""Support's service layer: the API and the pages call only these (AGENTS.md, rule 7)."""

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID
from psycopg.types.json import Jsonb
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mappd import db, durable, workflows

from . import domain
from .db import KB, TICKETS
from .schemas import (
    ApprovalResult,
    Article,
    Decision,
    DecisionResult,
    Escalation,
    Resolution,
    TicketIn,
    TicketOut,
    TicketPage,
)
from .workflow import approve_and_send, escalate_ticket, process_ticket, resolve_ticket

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from dbos import WorkflowHandleAsync


CATEGORY: sa.ColumnElement[str] = sa.func.coalesce(
    TICKETS.c.decision["corrected"]["topic"].astext, TICKETS.c.output["topic"].astext
)
EMPTY = sa.cast("{}", JSONB)
DECISIONS: dict[str, sa.ColumnElement[bool]] = {
    "auto": TICKETS.c.decision["operator"].astext == "auto",
    "edited": sa.and_(
        TICKETS.c.decision["kind"].astext == "send",
        TICKETS.c.decision["operator"].astext != "auto",
        sa.or_(
            TICKETS.c.decision["edited_reply"].astext == "true",
            TICKETS.c.decision["corrected"] != EMPTY,
        ),
    ),
    "unedited": sa.and_(
        TICKETS.c.decision["kind"].astext == "send",
        sa.func.coalesce(TICKETS.c.decision["edited_reply"].astext, "false") == "false",
        TICKETS.c.decision["corrected"] == EMPTY,
    ),
    # an agent escalation the operator judged unnecessary (not one an operator made)
    "false_escalation": sa.and_(
        TICKETS.c.decision["agent_could_handle"].astext == "true",
        TICKETS.c.decision["prior"].is_(None),
    ),
}


class NotFoundError(LookupError):
    """No ticket has this external_id."""


class StaleVersionError(Exception):
    """The decision named an old version, or the ticket is no longer awaiting approval."""

    def __init__(self, current_version: int, status: str) -> None:
        super().__init__(f"ticket is at version {current_version} ({status})")
        self.current_version = current_version
        self.status = status


class IdempotencyMismatchError(ValueError):
    """The idempotency key was used before with a different decision."""


class SendFailedError(RuntimeError):
    """The decision was claimed but the reply is not confirmed sent; its workflow ended in ERROR.

    Settle the send on /actions if its outcome is unknown, then `mappd workflow retry`.
    """

    def __init__(self, workflow_id: str) -> None:
        super().__init__(f"sending did not complete; retry workflow {workflow_id}")
        self.workflow_id = workflow_id


class HardRuleError(ValueError):
    """A resolution says the agent could have handled what a hard rule escalated."""


class BadCursorError(ValueError):
    """`before` is not a cursor this service issued."""


async def ingest(ticket: TicketIn) -> str:
    """Store a ticket and start its workflow; a redelivery starts nothing new."""
    external_id = await store(ticket)
    workflow_id = domain.ticket_workflow_id(external_id)
    await start(workflow_id, process_ticket, external_id)
    return workflow_id


@durable.step
async def store(ticket: TicketIn) -> str:
    """Store a ticket once; the key it is stored under (a step inside a mail poll).

    A redelivery changes nothing: the same `external_id`, or a mail whose Message-ID is
    already stored (under the key it got then), answers the stored ticket's key.
    """
    values = ticket.model_dump(exclude={"mail"})
    if ticket.mail is not None:
        values["mail"] = Jsonb(ticket.mail.model_dump(mode="json"))
    message_id = ticket.mail.message_id if ticket.mail is not None else None
    async with db.aconnect() as conn:
        stored = await (
            await db.arun(
                conn,
                pg_insert(TICKETS)
                .values(**values)
                .on_conflict_do_nothing()  # external_id, or the mail's Message-ID
                .returning(TICKETS.c.external_id),
            )
        ).fetchone()
        if stored is None:
            same = TICKETS.c.external_id == ticket.external_id
            if message_id is not None:
                same = sa.or_(same, TICKETS.c.mail["message_id"].astext == message_id)
            stored = await (
                await db.arun(conn, sa.select(TICKETS.c.external_id).where(same).limit(1))
            ).fetchone()
    if stored is None:  # pragma: no cover - a conflict means the row exists
        raise RuntimeError(f"ticket {ticket.external_id!r} was neither stored nor found")
    return str(stored["external_id"])


async def get_ticket(external_id: str) -> TicketOut:
    """The stored ticket.

    Raises:
        NotFoundError: no such ticket.
    """
    async with db.aconnect() as conn:
        row = await (
            await db.arun(conn, sa.select(TICKETS).where(TICKETS.c.external_id == external_id))
        ).fetchone()
    if row is None:
        raise NotFoundError(f"no support ticket {external_id!r}")
    found = TicketOut.model_validate(dict(row))
    if found.status == "new":  # still unprocessed: did its workflow stop?
        found.processing = await workflows.stopped(domain.ticket_workflow_id(external_id))
    return found


def _cursor(created_at: datetime, row_id: int) -> str:
    raw = json.dumps([created_at.isoformat(), row_id]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode(cursor: str) -> tuple[datetime, int]:
    try:
        created, row_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(created), int(row_id)
    except (binascii.Error, ValueError, TypeError) as e:
        raise BadCursorError(f"bad cursor {cursor!r}") from e


async def list_tickets(
    *,
    status: str | None = None,
    category: str | None = None,
    decision: str | None = None,
    limit: int = 50,
    before: str | None = None,
) -> TicketPage:
    """Tickets newest first, `limit` per page; `before` is the previous page's `next`.

    Raises:
        BadCursorError: `before` is malformed.
    """
    query = (
        sa.select(TICKETS)
        .order_by(TICKETS.c.created_at.desc(), TICKETS.c.id.desc())
        .limit(limit + 1)
    )
    if status not in (None, "all"):  # "all": every status, as the queue's tab
        query = query.where(TICKETS.c.status == status)
    if category is not None:
        query = query.where(CATEGORY.is_not_distinct_from(category))
    if decision is not None:
        if decision not in DECISIONS:
            raise ValueError(f"unknown decision filter {decision!r}; one of {', '.join(DECISIONS)}")
        query = query.where(DECISIONS[decision])
    if before is not None:
        created_at, row_id = _decode(before)
        query = query.where(sa.tuple_(TICKETS.c.created_at, TICKETS.c.id) < (created_at, row_id))
    async with db.aconnect() as conn:
        rows = await (await db.arun(conn, query)).fetchall()
    page, more = rows[:limit], len(rows) > limit
    last = page[-1] if page else None
    return TicketPage(
        items=[TicketOut.model_validate(dict(r)) for r in page],
        next=_cursor(last["created_at"], last["id"]) if more and last is not None else None,
    )


async def articles(article_ids: Sequence[str]) -> list[Article]:
    """Titles of the cited knowledge-base articles, in citation order."""
    if not article_ids:
        return []
    async with db.aconnect() as conn:
        rows = await (
            await db.arun(
                conn,
                # = ANY(array): mappd.db compiles once, without SQLAlchemy's expanding IN.
                sa.select(KB.c.id, KB.c.title).where(
                    KB.c.id == sa.any_(sa.bindparam("ids", list(article_ids), type_=ARRAY(sa.Text)))
                ),
            )
        ).fetchall()
    by_id = {r["id"]: Article(id=r["id"], title=r["title"]) for r in rows}
    return [by_id[i] for i in article_ids if i in by_id]


async def approve(
    external_id: str, decision: Decision, *, idempotency_key: str | None = None
) -> ApprovalResult:
    """Apply an operator's decision once.

    Without a key the decision's ID is the ticket and version (a double click is one run);
    with a key, the key. A decision that ran before answers its stored result with
    `repeated=True`.

    Raises:
        NotFoundError: no such ticket (no workflow starts).
        IdempotencyMismatchError: the key was used with a different decision.
        StaleVersionError: the version is old or the ticket moved on.
        SendFailedError: the decision was claimed but the send did not complete.
    """
    await get_ticket(external_id)
    workflow_id = domain.decision_workflow_id(
        "support.approve", external_id, decision.version, idempotency_key
    )
    args = (external_id, decision.version, decision.operator, decision.reply, decision.topic)
    result, repeated = await _decide(workflow_id, approve_and_send, args, external_id)
    return ApprovalResult(result=result, repeated=repeated)


async def escalate(
    external_id: str, e: Escalation, *, idempotency_key: str | None = None
) -> DecisionResult:
    """Hand an awaiting ticket to a team instead of sending the draft (raises like approve)."""
    t = await get_ticket(external_id)
    record = domain.escalation_record(
        e, agent_topic=t.output.topic if t.output else None, now=datetime.now(UTC)
    ).stored()
    wid = domain.decision_workflow_id("support.escalate", external_id, e.version, idempotency_key)
    result, repeated = await _decide(
        wid, escalate_ticket, (external_id, e.version, record), external_id
    )
    return DecisionResult(result=result, repeated=repeated)


async def resolve(
    external_id: str, r: Resolution, *, idempotency_key: str | None = None
) -> DecisionResult:
    """Close an escalated ticket and judge the escalation (raises like approve).

    Raises:
        HardRuleError: `agent_could_handle`, or another outcome, for a hard-rule escalation.
    """
    t = await get_ticket(external_id)
    rule = domain.escalated_by_rule(t.output)
    if rule is not None and (r.agent_could_handle or r.outcome != "escalate"):
        raise HardRuleError(f"a hard rule escalated it, so escalating was right: {rule}")
    record = domain.resolution_record(
        r, agent=t.output, now=datetime.now(UTC), prior=t.decision
    ).stored()
    wid = domain.decision_workflow_id("support.resolve", external_id, r.version, idempotency_key)
    result, repeated = await _decide(
        wid, resolve_ticket, (external_id, r.version, record), external_id
    )
    return DecisionResult(result=result, repeated=repeated)


async def _decide(
    workflow_id: str,
    func: Callable[..., Coroutine[Any, Any, str]],
    args: tuple[object, ...],
    external_id: str,
) -> tuple[str, bool]:
    """Run a decision workflow once: (result, repeated).

    Raises:
        IdempotencyMismatchError: the ID was used with another decision.
        StaleVersionError: the version is old or the ticket moved on.
        SendFailedError: the workflow ended in ERROR.
    """
    repeated = await _ran_before(workflow_id, args, external_id)
    handle: WorkflowHandleAsync[str] = (
        await DBOS.retrieve_workflow_async(workflow_id)
        if repeated
        else await start(workflow_id, func, *args)
    )
    if not repeated:
        # Two callers can both find no workflow and both start: DBOS attaches the second to
        # the first without comparing inputs. Look again, so a different decision is refused.
        await _ran_before(workflow_id, args, external_id)
    try:
        result = await handle.get_result()
    except Exception as e:
        raise SendFailedError(workflow_id) from e
    if result == "conflict":
        current = await get_ticket(external_id)
        raise StaleVersionError(current.version, current.status)
    return result, repeated


def _comparable(args: Sequence[object]) -> tuple[object, ...]:
    """Decision inputs as compared for repeats: a record's timestamp is not part of it."""
    return tuple(
        {k: v for k, v in a.items() if k != "decided_at"} if isinstance(a, dict) else a
        for a in args
    )


async def _ran_before(workflow_id: str, args: tuple[object, ...], external_id: str) -> bool:
    """Whether `workflow_id` exists; raises if it was started with another decision."""
    existing = await DBOS.get_workflow_status_async(workflow_id)
    if existing is None:
        return False
    stored = tuple(existing.input["args"]) if existing.input is not None else ()
    if _comparable(stored) != _comparable(args):
        raise IdempotencyMismatchError(
            f"this idempotency key was used for another decision on {external_id}"
        )
    return True


async def start[**P, R](
    workflow_id: str,
    func: Callable[P, Coroutine[Any, Any, R]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> WorkflowHandleAsync[R]:
    """Start `func` under `workflow_id`; retries and redeliveries collapse onto it."""
    with SetWorkflowID(workflow_id):
        return await DBOS.start_workflow_async(func, *args, **kwargs)
