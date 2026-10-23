"""Orchestration of the RFQ process: two short workflows (AGENTS.md, rule 3).

The same shape as support's: process_rfq reads, decides and saves, and sends by
policy through a child workflow; approve_and_send claims the decision at its version, reads
shadow mode after the claim, sends the quote once and marks it sent. Escalate and resolve are
decisions without a send.
"""

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID
from psycopg.types.json import Jsonb

from mappd import db, durable, mail, telemetry
from mappd.autonomy import Policy
from mappd.events import emit
from mappd.locking import update_if_version

from . import domain
from .db import RFQS
from .events import WEBHOOKS, event
from .schemas import LineFix, RfqOutput, RfqRecord
from .solve import solve

AUTO = "auto"


def current_policy() -> Policy:
    """The RFQ policy as loaded at startup (tests patch this)."""
    from . import process  # noqa: PLC0415 - the package imports this module first

    return process.policy()


@durable.step
async def autonomy_level(category: str) -> str:
    """The outcome's level, journaled: a replay sees the level recorded at the time."""
    return current_policy().level(category)


@durable.step
async def shadow_mode() -> bool:
    """Whether the process sends nothing, journaled like `autonomy_level`."""
    return current_policy().shadow


@durable.workflow
async def process_rfq(external_id: str) -> str:
    """Extract and price a request; escalate it, or hold the quote or question for approval."""
    rfq = await load_rfq(external_id)
    await emit(event("created", rfq, rfq.version, rfq.status), WEBHOOKS)
    with telemetry.traced(
        "rfq.process_rfq",
        inputs=rfq,
        user=rfq.sender,
        session=external_id,
        tags={"rfq.external_id": external_id},
    ) as note:
        output = await solve(rfq.rfq_in)
        status = "escalated" if output.outcome == "escalate" else "awaiting_approval"
        await save_outcome(external_id, status, output)
        kind = "escalated" if status == "escalated" else "approval_needed"
        await emit(event(kind, rfq, rfq.version + 1, status, output), WEBHOOKS)
        if output.draft is not None and domain.auto_sends(
            output.outcome, await autonomy_level(output.outcome)
        ):
            # A child the parent does not wait for (rule 3); its ID is the decision's.
            approval = domain.approval_workflow_id(external_id, rfq.version + 1)
            with SetWorkflowID(approval):
                await DBOS.start_workflow_async(
                    approve_and_send, external_id, rfq.version + 1, AUTO, output.draft
                )
        note.outputs = {"status": status, **output.model_dump(mode="json")}
        return status


@durable.workflow
async def approve_and_send(
    external_id: str,
    version: int,
    operator: str,
    reply: str | None,
    lines: list[dict[str, Any]] | None = None,
) -> str:
    """Apply a decision and send the quote once; corrected `lines` re-price it first.

    `reply` None, or the agent's draft unchanged, sends the (re-priced) draft.
    """
    with telemetry.traced(
        "rfq.approve_and_send",
        inputs={"external_id": external_id, "version": version, "operator": operator},
        user=operator,
        session=external_id,
        tags={"rfq.external_id": external_id},
    ) as note:
        rfq = await load_rfq(external_id)
        fixes = [LineFix.model_validate(f) for f in lines] if lines is not None else None
        draft = rfq.output.draft if rfq.output else None
        record = domain.send_record(
            operator=operator,
            reply=reply,
            draft=draft,
            lines=fixes,
            agent=domain.agent_lines(rfq.output),
            now=datetime.now(UTC),
        )
        output = rfq.output
        if record.corrected.lines is not None and rfq.output is not None:
            # Validated by the service; checked again so a replayed or forked run cannot send a
            # quote the lines do not support (ValueError fails the workflow, never retried).
            output = domain.check_fixes(rfq.output, record.corrected.lines)
        body = domain.reply_body(rfq.output, output, reply)
        stored = output.model_dump(mode="json") if output is not rfq.output and output else None
        if not await claim_approval(
            rfq.id, version, operator, body, record=record.stored(), output=stored
        ):
            note.outputs = {"result": "conflict"}
            return "conflict"
        # Read after the claim, as support does, so the journal starts load, claim.
        if await shadow_mode():
            await mark_shadowed(external_id)
            await emit(event("closed", rfq, version + 2, "shadowed"), WEBHOOKS)
            note.outputs = {"result": "shadowed"}
            return "shadowed"
        context = (
            rfq.mail.reply_context(rfq.subject, rfq.sender, rfq.body)
            if rfq.mail is not None
            else None
        )
        message_id = await mail.send(
            external_id,
            mailbox="rfq",
            to=rfq.sender,
            subject=rfq.subject,
            text=body,
            reply_to=context,
            automatic=operator == AUTO,
        )
        await mark_sent(external_id, message_id)
        await emit(event("closed", rfq, version + 2, "sent", output), WEBHOOKS)
        note.outputs = {"result": "sent", "to": rfq.sender}
        return "sent"


@durable.step
async def load_rfq(external_id: str) -> RfqRecord:
    """The request as stored."""
    async with db.aconnect() as conn:
        row = await (
            await db.arun(conn, sa.select(RFQS).where(RFQS.c.external_id == external_id))
        ).fetchone()
    if row is None:
        raise LookupError(f"no RFQ {external_id!r}")
    return RfqRecord.model_validate(dict(row))


@durable.transaction
def save_outcome(conn: db.Connection, external_id: str, status: str, output: RfqOutput) -> None:
    """Store the decision and move the request on; a replay matches no row."""
    db.run(
        conn,
        sa.update(RFQS)
        .where(RFQS.c.external_id == external_id, RFQS.c.status == "new")
        .values(
            status=status,
            output=Jsonb(output.model_dump(mode="json")),
            version=RFQS.c.version + 1,
            updated_at=sa.func.now(),
        ),
    )


@durable.transaction
def claim_approval(
    conn: db.Connection,
    rfq_id: int,
    version: int,
    operator: str,
    reply: str,
    *,
    record: dict[str, Any],
    output: dict[str, Any] | None = None,
) -> bool:
    """Only a decision made on the current version, while awaiting approval, wins.

    `output` replaces the agent's quote when the operator corrected the lines.
    """
    changes: dict[str, object] = {
        "status": "approved",
        "approved_by": operator,
        "reply": reply,
        "decision": record,
    }
    if output is not None:
        changes["output"] = output
    return update_if_version(
        conn, "rfq_requests", rfq_id, version, changes, where={"status": "awaiting_approval"}
    )


@durable.transaction
def mark_sent(conn: db.Connection, external_id: str, message_id: str) -> None:
    """The quote is out under `message_id`; a replay finds it sent and changes nothing."""
    sent = sa.func.jsonb_build_object("sent_message_id", message_id)
    db.run(
        conn,
        sa.update(RFQS)
        .where(RFQS.c.external_id == external_id, RFQS.c.status == "approved")
        .values(
            status="sent",
            mail=sa.func.coalesce(RFQS.c.mail, sa.text("'{}'::jsonb")).op("||")(sent),
            version=RFQS.c.version + 1,
            updated_at=sa.func.now(),
        ),
    )


@durable.transaction
def mark_shadowed(conn: db.Connection, external_id: str) -> None:
    """Shadow mode: the decision is recorded and nothing went out; a replay changes nothing."""
    db.run(
        conn,
        sa.update(RFQS)
        .where(RFQS.c.external_id == external_id, RFQS.c.status == "approved")
        .values(
            status="shadowed",
            decision=sa.func.jsonb_set(
                RFQS.c.decision,
                sa.literal_column("'{shadow}'::text[]"),
                sa.literal_column("'true'::jsonb"),
            ),
            version=RFQS.c.version + 1,
            updated_at=sa.func.now(),
        ),
    )


@durable.workflow
async def escalate_rfq(external_id: str, version: int, record: dict[str, Any]) -> str:
    """An operator hands an awaiting request to a team; nothing is sent."""
    rfq = await load_rfq(external_id)
    if not await claim_decision(
        rfq.id, version, record, expected="awaiting_approval", status="escalated"
    ):
        return "conflict"
    team = record["corrected"]["escalate_to"]
    await emit(
        event("escalated", rfq, version + 1, "escalated", rfq.output, escalate_to=team), WEBHOOKS
    )
    return "escalated"


@durable.workflow
async def resolve_rfq(external_id: str, version: int, record: dict[str, Any]) -> str:
    """An operator closes an escalated request and says whether the agent could have quoted it."""
    rfq = await load_rfq(external_id)
    if not await claim_decision(rfq.id, version, record, expected="escalated", status="resolved"):
        return "conflict"
    await emit(event("closed", rfq, version + 1, "resolved"), WEBHOOKS)
    return "resolved"


@durable.transaction
def claim_decision(
    conn: db.Connection,
    rfq_id: int,
    version: int,
    record: dict[str, Any],
    *,
    expected: str,
    status: str,
) -> bool:
    """Only a decision on the current version of a request in `expected` wins."""
    return update_if_version(
        conn,
        "rfq_requests",
        rfq_id,
        version,
        {"status": status, "decision": record, "approved_by": record["operator"]},
        where={"status": expected},
    )
