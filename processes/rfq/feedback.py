"""RFQ decisions as eval cases (as support's, with lines instead of a category)."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel

from mappd import db
from mappd.data.cases import Case
from mappd.feedback import FeedbackSpec

from . import domain
from .db import RFQS
from .schemas import DecisionRecord, RfqOutput


class RfqRow(BaseModel):
    """What feedback needs of a decided request."""

    external_id: str
    sender: str
    subject: str
    body: str
    output: RfqOutput | None
    decision: DecisionRecord


async def collect(since: datetime) -> list[RfqRow]:
    """Requests decided since `since` (auto decisions are left out by to_case)."""
    decided_at = sa.cast(RFQS.c.decision["decided_at"].astext, sa.DateTime(timezone=True))
    query = sa.select(RFQS).where(RFQS.c.decision.is_not(None), decided_at >= since)
    async with db.aconnect() as conn:
        rows = await (await db.arun(conn, query)).fetchall()
    return [RfqRow.model_validate(dict(r)) for r in rows]


def to_case(row: RfqRow) -> Case | None:
    """The case a person's decision implies; None for auto decisions or requests without output."""
    d = row.decision
    if d.operator == "auto" or row.output is None:
        return None
    agent = row.output
    prior = d.prior.corrected.model_dump(exclude_none=True) if d.prior else {}
    corrected = d.corrected.model_dump(exclude_none=True)
    fixes = d.corrected.lines if d.corrected.lines is not None else domain.agent_lines(agent)
    priced = domain.reprice(agent, fixes) if d.corrected.lines is not None else agent
    expected: dict[str, Any] = {
        "outcome": agent.outcome,
        "escalate_to": agent.escalate_to,
        "language": agent.language,
        "needed_by_days": agent.needed_by_days,
        "lines": [f.model_dump() for f in fixes],
        "lines_key": domain.lines_key(fixes),
        "total": str(priced.total),
        "alternatives": {
            line.sku: line.alternative for line in priced.lines if line.sku and line.alternative
        },
    }
    if d.corrected.lines is not None:  # the operator's lines decide the outcome too
        expected["outcome"], expected["escalate_to"] = priced.outcome, priced.escalate_to
    if d.kind == "escalate":
        expected["outcome"] = "escalate"
    for fix in (prior, corrected):  # an operator escalation first, then its resolution
        expected.update({k: v for k, v in fix.items() if k in ("escalate_to",)})
    if d.kind == "resolve":
        expected["outcome"] = corrected.get("outcome", "escalate")
    if expected["outcome"] != "escalate":
        expected["escalate_to"] = None
    changed = sorted({**prior, **corrected})
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
            "corrected_fields": changed,
        },
    )


spec = FeedbackSpec(collect=collect, to_case=to_case)
