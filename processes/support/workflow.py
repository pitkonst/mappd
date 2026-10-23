"""Orchestration of the support process: two short workflows (AGENTS.md, rule 3).

Reads are async steps awaited directly. The durable writes stay sync — they run on the
DBOS datasource's connection so each write commits with its checkpoint — and the
`durable` decorators run them on a worker thread, so the event loop never blocks.

process_ticket: load → solve (classify → decide → draft) → save. Ends in seconds with the
ticket waiting in support_tickets. approve_and_send: claim the operator's decision at its version
→ send the reply → mark sent. Its ID is the decision, so retries and double clicks run once.
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
from .agents import classifier, drafter
from .db import TICKETS
from .events import WEBHOOKS, event
from .schemas import Category, SupportOutput, TicketIn, TicketRecord

AUTO = "auto"


def current_policy() -> Policy:
    """The support policy as loaded at startup (tests patch this)."""
    from . import process  # noqa: PLC0415 - the package imports this module first

    return process.policy()


@durable.step
async def autonomy_level(category: str) -> str:
    """The category's level, journaled: a replay sees the level recorded at the time."""
    return current_policy().level(category)


@durable.step
async def shadow_mode() -> bool:
    """Whether the process sends nothing, journaled like `autonomy_level`."""
    return current_policy().shadow


@durable.workflow
async def process_ticket(external_id: str) -> str:
    """Classify a ticket and either escalate it or draft a reply for approval."""
    ticket = await load_ticket(external_id)
    await emit(event("created", ticket, ticket.version, ticket.status), WEBHOOKS)
    with telemetry.traced(
        "support.process_ticket",
        inputs=ticket,
        user=ticket.sender,
        session=external_id,
        tags={"ticket.external_id": external_id},
    ) as note:
        output = await solve(ticket.ticket_in)
        status = "escalated" if output.outcome == "escalate" else "awaiting_approval"
        await save_outcome(external_id, status, output)
        kind = "escalated" if status == "escalated" else "approval_needed"
        await emit(event(kind, ticket, ticket.version + 1, status, output), WEBHOOKS)
        if output.draft is not None and domain.auto_sends(
            output.outcome, await autonomy_level(output.topic)
        ):
            # A child the parent does not wait for (rule 3); its ID is the decision's.
            approval = domain.approval_workflow_id(external_id, ticket.version + 1)
            with SetWorkflowID(approval):
                await DBOS.start_workflow_async(
                    approve_and_send, external_id, ticket.version + 1, AUTO, output.draft.reply
                )
        note.outputs = {"status": status, **output.model_dump(mode="json")}
        return status


@durable.workflow
async def approve_and_send(
    external_id: str, version: int, operator: str, reply: str, topic: Category | None = None
) -> str:
    """Apply a decision (an operator's, or `auto`) and send the reply once; shadow sends nothing."""
    with telemetry.traced(
        "support.approve_and_send",
        inputs={
            "external_id": external_id,
            "version": version,
            "operator": operator,
            "reply": reply,
        },
        user=operator,
        session=external_id,
        tags={"ticket.external_id": external_id},
    ) as note:
        ticket = await load_ticket(external_id)
        draft = ticket.output.draft if ticket.output else None
        record = domain.send_record(
            operator=operator,
            shadow=False,  # set by mark_shadowed: the claim must stay step 1, as in 3b
            reply=reply,
            draft=draft.reply if draft else None,
            topic=topic,
            agent_topic=ticket.output.topic if ticket.output else None,
            now=datetime.now(UTC),
        )
        if not await claim_approval(ticket.id, version, operator, reply, record=record.stored()):
            note.outputs = {"result": "conflict"}
            return "conflict"
        # Read after the claim, so the journal starts load_ticket, claim_approval like a 3b
        # approval's and `mappd workflow retry` of one made before 3c still replays.
        if await shadow_mode():
            await mark_shadowed(external_id)
            await emit(event("closed", ticket, version + 2, "shadowed"), WEBHOOKS)
            note.outputs = {"result": "shadowed"}
            return "shadowed"
        context = (
            ticket.mail.reply_context(ticket.subject, ticket.sender, ticket.body)
            if ticket.mail is not None
            else None
        )
        message_id = await mail.send(
            external_id,
            mailbox="support",
            to=ticket.sender,
            subject=ticket.subject,
            text=reply,
            reply_to=context,
            automatic=operator == AUTO,
        )
        await mark_sent(external_id, message_id)
        await emit(event("closed", ticket, version + 2, "sent"), WEBHOOKS)
        note.outputs = {"result": "sent", "to": ticket.sender, "reply": reply}
        return "sent"


async def solve(ticket: TicketIn) -> SupportOutput:
    """Classify, decide, and draft a reply or a clarifying question."""
    text = domain.ticket_text(ticket.subject, ticket.body)
    classification = (await classifier.run(text)).output
    outcome, role = domain.decide(classification)
    draft = None
    if outcome != "escalate":
        prompt = domain.draft_prompt(text, outcome, classification.language)
        draft = (await drafter.run(prompt)).output
    return SupportOutput(
        topic=classification.topic,
        route=classification.route,
        urgency=classification.urgency,
        language=classification.language,
        outcome=outcome,
        escalate_to=role,
        draft=draft,
    )


@durable.step
async def load_ticket(external_id: str) -> TicketRecord:
    """The ticket as stored."""
    async with db.aconnect() as conn:
        row = await (
            await db.arun(conn, sa.select(TICKETS).where(TICKETS.c.external_id == external_id))
        ).fetchone()
    if row is None:
        raise LookupError(f"no support ticket {external_id!r}")
    return TicketRecord.model_validate(dict(row))


@durable.transaction
def save_outcome(conn: db.Connection, external_id: str, status: str, output: SupportOutput) -> None:
    """Store the decision and move the ticket to its next status; a replay matches no row."""
    db.run(
        conn,
        sa.update(TICKETS)
        .where(TICKETS.c.external_id == external_id, TICKETS.c.status == "new")
        .values(
            status=status,
            output=Jsonb(output.model_dump(mode="json")),
            version=TICKETS.c.version + 1,
            updated_at=sa.func.now(),
        ),
    )


@durable.transaction
def claim_approval(
    conn: db.Connection,
    ticket_id: int,
    version: int,
    operator: str,
    reply: str,
    *,
    record: dict[str, Any],
) -> bool:
    """Only a decision made on the current version, while awaiting approval, wins."""
    return update_if_version(
        conn,
        "support_tickets",
        ticket_id,
        version,
        {"status": "approved", "approved_by": operator, "reply": reply, "decision": record},
        where={"status": "awaiting_approval"},
    )


@durable.transaction
def mark_sent(conn: db.Connection, external_id: str, message_id: str) -> None:
    """The reply is out under `message_id`; a replay finds the ticket sent, changes nothing."""
    sent = sa.func.jsonb_build_object("sent_message_id", message_id)
    db.run(
        conn,
        sa.update(TICKETS)
        .where(TICKETS.c.external_id == external_id, TICKETS.c.status == "approved")
        .values(
            status="sent",
            mail=sa.func.coalesce(TICKETS.c.mail, sa.text("'{}'::jsonb")).op("||")(sent),
            version=TICKETS.c.version + 1,
            updated_at=sa.func.now(),
        ),
    )


@durable.transaction
def mark_shadowed(conn: db.Connection, external_id: str) -> None:
    """Shadow mode: the decision is recorded and nothing went out; a replay changes nothing."""
    db.run(
        conn,
        sa.update(TICKETS)
        .where(TICKETS.c.external_id == external_id, TICKETS.c.status == "approved")
        .values(
            status="shadowed",
            decision=sa.func.jsonb_set(
                TICKETS.c.decision,
                sa.literal_column("'{shadow}'::text[]"),
                sa.literal_column("'true'::jsonb"),
            ),
            version=TICKETS.c.version + 1,
            updated_at=sa.func.now(),
        ),
    )


@durable.workflow
async def escalate_ticket(external_id: str, version: int, record: dict[str, Any]) -> str:
    """An operator hands an awaiting ticket to a team; nothing is sent."""
    ticket = await load_ticket(external_id)
    if not await claim_decision(
        ticket.id, version, record, expected="awaiting_approval", status="escalated"
    ):
        return "conflict"
    team = record["corrected"]["escalate_to"]
    await emit(
        event("escalated", ticket, version + 1, "escalated", ticket.output, escalate_to=team),
        WEBHOOKS,
    )
    return "escalated"


@durable.workflow
async def resolve_ticket(external_id: str, version: int, record: dict[str, Any]) -> str:
    """An operator closes an escalated ticket and says whether the agent could have handled it."""
    ticket = await load_ticket(external_id)
    if not await claim_decision(
        ticket.id, version, record, expected="escalated", status="resolved"
    ):
        return "conflict"
    await emit(event("closed", ticket, version + 1, "resolved"), WEBHOOKS)
    return "resolved"


@durable.transaction
def claim_decision(
    conn: db.Connection,
    ticket_id: int,
    version: int,
    record: dict[str, Any],
    *,
    expected: str,
    status: str,
) -> bool:
    """Only a decision on the current version of a ticket in `expected` wins."""
    return update_if_version(
        conn,
        "support_tickets",
        ticket_id,
        version,
        {"status": status, "decision": record, "approved_by": record["operator"]},
        where={"status": expected},
    )
