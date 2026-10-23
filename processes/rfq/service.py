"""RFQ's service layer: the API and the pages call only these (AGENTS.md, rule 7)."""

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID
from psycopg.types.json import Jsonb
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mappd import db, durable, workflows

from . import domain
from .db import RFQS
from .schemas import (
    ApprovalResult,
    Decision,
    DecisionResult,
    Escalation,
    Resolution,
    RfqIn,
    RfqOut,
    RfqPage,
)
from .workflow import approve_and_send, escalate_rfq, process_rfq, resolve_rfq

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from dbos import WorkflowHandleAsync


# The autonomy category of a request is its outcome (quote, clarify).
CATEGORY: sa.ColumnElement[str] = RFQS.c.output["outcome"].astext
EMPTY = sa.cast("{}", JSONB)
DECISIONS: dict[str, sa.ColumnElement[bool]] = {
    "auto": RFQS.c.decision["operator"].astext == "auto",
    "edited": sa.and_(
        RFQS.c.decision["kind"].astext == "send",
        RFQS.c.decision["operator"].astext != "auto",
        sa.or_(
            RFQS.c.decision["edited_reply"].astext == "true",
            RFQS.c.decision["corrected"] != EMPTY,
        ),
    ),
    "unedited": sa.and_(
        RFQS.c.decision["kind"].astext == "send",
        sa.func.coalesce(RFQS.c.decision["edited_reply"].astext, "false") == "false",
        RFQS.c.decision["corrected"] == EMPTY,
    ),
    # an agent escalation the operator judged unnecessary (not one an operator made)
    "false_escalation": sa.and_(
        RFQS.c.decision["agent_could_handle"].astext == "true",
        RFQS.c.decision["prior"].is_(None),
    ),
}


class NotFoundError(LookupError):
    """No rfq has this external_id."""


class StaleVersionError(Exception):
    """The decision named an old version, or the rfq is no longer awaiting approval."""

    def __init__(self, current_version: int, status: str) -> None:
        super().__init__(f"rfq is at version {current_version} ({status})")
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


class InvalidLinesError(ValueError):
    """Corrected lines that cannot go out as a quote: unknown, none, or over the threshold."""


class InvalidReplyError(ValueError):
    """A reply that changes the quote's figures, or no reply when there is no draft."""


class HardRuleError(ValueError):
    """A resolution says the agent could have handled what a hard rule escalated."""


class BadCursorError(ValueError):
    """`before` is not a cursor this service issued."""


async def ingest(rfq: RfqIn) -> str:
    """Store a rfq and start its workflow; a redelivery starts nothing new."""
    external_id = await store(rfq)
    workflow_id = domain.rfq_workflow_id(external_id)
    await start(workflow_id, process_rfq, external_id)
    return workflow_id


@durable.step
async def store(rfq: RfqIn) -> str:
    """Store a rfq once; the key it is stored under (a step inside a mail poll).

    A redelivery changes nothing: the same `external_id`, or a mail whose Message-ID is
    already stored (under the key it got then), answers the stored rfq's key.
    """
    values = rfq.model_dump(exclude={"mail"})
    if rfq.mail is not None:
        values["mail"] = Jsonb(rfq.mail.model_dump(mode="json"))
    message_id = rfq.mail.message_id if rfq.mail is not None else None
    async with db.aconnect() as conn:
        stored = await (
            await db.arun(
                conn,
                pg_insert(RFQS)
                .values(**values)
                .on_conflict_do_nothing()  # external_id, or the mail's Message-ID
                .returning(RFQS.c.external_id),
            )
        ).fetchone()
        if stored is None:
            same = RFQS.c.external_id == rfq.external_id
            if message_id is not None:
                same = sa.or_(same, RFQS.c.mail["message_id"].astext == message_id)
            stored = await (
                await db.arun(conn, sa.select(RFQS.c.external_id).where(same).limit(1))
            ).fetchone()
    if stored is None:  # pragma: no cover - a conflict means the row exists
        raise RuntimeError(f"rfq {rfq.external_id!r} was neither stored nor found")
    return str(stored["external_id"])


async def get_rfq(external_id: str) -> RfqOut:
    """The stored rfq.

    Raises:
        NotFoundError: no such rfq.
    """
    async with db.aconnect() as conn:
        row = await (
            await db.arun(conn, sa.select(RFQS).where(RFQS.c.external_id == external_id))
        ).fetchone()
    if row is None:
        raise NotFoundError(f"no RFQ {external_id!r}")
    found = RfqOut.model_validate(dict(row))
    if found.status == "new":  # still unprocessed: did its workflow stop?
        found.processing = await workflows.stopped(domain.rfq_workflow_id(external_id))
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


async def list_rfqs(
    *,
    status: str | None = None,
    category: str | None = None,
    decision: str | None = None,
    limit: int = 50,
    before: str | None = None,
) -> RfqPage:
    """Rfqs newest first, `limit` per page; `before` is the previous page's `next`.

    Raises:
        BadCursorError: `before` is malformed.
    """
    query = sa.select(RFQS).order_by(RFQS.c.created_at.desc(), RFQS.c.id.desc()).limit(limit + 1)
    if status not in (None, "all"):  # "all": every status, as the queue's tab
        query = query.where(RFQS.c.status == status)
    if category is not None:
        query = query.where(CATEGORY.is_not_distinct_from(category))
    if decision is not None:
        if decision not in DECISIONS:
            raise ValueError(f"unknown decision filter {decision!r}; one of {', '.join(DECISIONS)}")
        query = query.where(DECISIONS[decision])
    if before is not None:
        created_at, row_id = _decode(before)
        query = query.where(sa.tuple_(RFQS.c.created_at, RFQS.c.id) < (created_at, row_id))
    async with db.aconnect() as conn:
        rows = await (await db.arun(conn, query)).fetchall()
    page, more = rows[:limit], len(rows) > limit
    last = page[-1] if page else None
    return RfqPage(
        items=[RfqOut.model_validate(dict(r)) for r in page],
        next=_cursor(last["created_at"], last["id"]) if more and last is not None else None,
    )


async def approve(
    external_id: str, decision: Decision, *, idempotency_key: str | None = None
) -> ApprovalResult:
    """Apply an operator's decision once.

    Without a key the decision's ID is the rfq and version (a double click is one run);
    with a key, the key. A decision that ran before answers its stored result with
    `repeated=True`.

    Raises:
        NotFoundError: no such rfq (no workflow starts).
        IdempotencyMismatchError: the key was used with a different decision.
        StaleVersionError: the version is old or the rfq moved on.
        SendFailedError: the decision was claimed but the send did not complete.
        InvalidLinesError: corrected lines that are not a plain quote (no workflow starts).
        InvalidReplyError: the reply changes the quote's figures (no workflow starts).
    """
    t = await get_rfq(external_id)
    priced = t.output
    if decision.lines is not None:
        if t.output is None:
            raise InvalidLinesError("the request has no quote to correct yet")
        try:
            priced = domain.check_fixes(t.output, decision.lines)
        except ValueError as e:
            raise InvalidLinesError(str(e)) from e
    try:
        domain.reply_body(t.output, priced, decision.reply)
    except ValueError as e:
        raise InvalidReplyError(str(e)) from e
    workflow_id = domain.decision_workflow_id(
        "rfq.approve", external_id, decision.version, idempotency_key
    )
    lines = [f.model_dump() for f in decision.lines] if decision.lines is not None else None
    args = (external_id, decision.version, decision.operator, decision.reply, lines)
    result, repeated = await _decide(workflow_id, approve_and_send, args, external_id)
    return ApprovalResult(result=result, repeated=repeated)


async def reply_problem(external_id: str, reply: str | None) -> str | None:
    """Why `reply` could not go out as this request's quote, or None when it could.

    The same check `approve` makes, for a form that failed on other fields, so every error
    shows at once. Lines are not considered (the page cannot correct them).
    """
    t = await get_rfq(external_id)
    try:
        domain.reply_body(t.output, t.output, reply or None)
    except ValueError as e:
        return str(e)
    return None


async def escalate(
    external_id: str, e: Escalation, *, idempotency_key: str | None = None
) -> DecisionResult:
    """Hand an awaiting rfq to a team instead of sending the draft (raises like approve)."""
    t = await get_rfq(external_id)
    del t  # the request must exist; escalation takes nothing from the agent's output
    record = domain.escalation_record(e, now=datetime.now(UTC)).stored()
    wid = domain.decision_workflow_id("rfq.escalate", external_id, e.version, idempotency_key)
    result, repeated = await _decide(
        wid, escalate_rfq, (external_id, e.version, record), external_id
    )
    return DecisionResult(result=result, repeated=repeated)


async def resolve(
    external_id: str, r: Resolution, *, idempotency_key: str | None = None
) -> DecisionResult:
    """Close an escalated rfq and judge the escalation (raises like approve).

    Raises:
        HardRuleError: `agent_could_handle`, or another outcome, for a hard-rule escalation.
    """
    t = await get_rfq(external_id)
    rule = domain.escalated_by_rule(t.output)
    if rule is not None and (r.agent_could_handle or r.outcome != "escalate"):
        raise HardRuleError(f"a hard rule escalated it, so escalating was right: {rule}")
    record = domain.resolution_record(
        r, agent=t.output, now=datetime.now(UTC), prior=t.decision
    ).stored()
    wid = domain.decision_workflow_id("rfq.resolve", external_id, r.version, idempotency_key)
    result, repeated = await _decide(
        wid, resolve_rfq, (external_id, r.version, record), external_id
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
        StaleVersionError: the version is old or the rfq moved on.
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
        current = await get_rfq(external_id)
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
