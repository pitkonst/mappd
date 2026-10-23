"""Support decisions as eval cases."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel

from mappd import db
from mappd.data.cases import Case
from mappd.feedback import FeedbackSpec

from .db import TICKETS
from .schemas import DecisionRecord, SupportOutput

LABELS = ("topic", "route", "urgency", "language", "outcome", "escalate_to")


class TicketRow(BaseModel):
    """What feedback needs of a decided ticket."""

    external_id: str
    sender: str
    subject: str
    body: str
    output: SupportOutput | None
    decision: DecisionRecord


async def collect(since: datetime) -> list[TicketRow]:
    """Tickets a person decided since `since` (auto decisions are left out by to_case)."""
    decided_at = sa.cast(TICKETS.c.decision["decided_at"].astext, sa.DateTime(timezone=True))
    query = sa.select(TICKETS).where(TICKETS.c.decision.is_not(None), decided_at >= since)
    async with db.aconnect() as conn:
        rows = await (await db.arun(conn, query)).fetchall()
    return [TicketRow.model_validate(dict(r)) for r in rows]


def to_case(row: TicketRow) -> Case | None:
    """The case a person's decision implies; None for auto decisions or tickets without output."""
    d = row.decision
    if d.operator == "auto" or row.output is None:
        return None
    agent = row.output
    expected: dict[str, Any] = {k: getattr(agent, k) for k in LABELS}
    prior = d.prior.corrected.model_dump(exclude_none=True) if d.prior else {}
    corrected = d.corrected.model_dump(exclude_none=True)
    if d.kind == "escalate":
        expected["outcome"] = "escalate"
    elif d.kind == "resolve":
        expected["outcome"] = corrected.get("outcome", "escalate")
    for fix in (prior, corrected):  # an operator escalation first, then its resolution
        expected.update({k: v for k, v in fix.items() if k != "outcome"})
    if d.kind == "resolve":
        expected["outcome"] = corrected.get("outcome", "escalate")
    if expected["outcome"] != "escalate":
        expected["escalate_to"] = None
    # the reply went out as drafted: its citations stand, whatever else was corrected
    unedited_send = d.kind == "send" and not d.edited_reply
    expected["citations"] = list(agent.draft.citations) if unedited_send and agent.draft else []
    corrected = {**prior, **corrected}
    return Case(
        id=f"fb-{row.external_id}",
        input={
            "external_id": row.external_id,
            "sender": row.sender,
            "subject": row.subject,
            "body": row.body,
        },
        expected=expected,
        meta={
            "source": "feedback",
            "ticket": row.external_id,
            "decided_at": d.decided_at.isoformat(),
            "kind": d.kind,
            "edited": d.edited_reply or bool(corrected),
            "corrected_fields": sorted(corrected),
        },
    )


spec = FeedbackSpec(collect=collect, to_case=to_case)
