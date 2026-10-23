"""Trust numbers per category: approved without edits next to false escalations.

Proposals are the agent's decided proposals — sends
(operator or auto) and operator escalations of an awaiting ticket; without edits are auto
sends plus operator sends with no edit and no correction; false escalations are resolutions
marked `agent_could_handle` over all resolutions.

"Without edits" judges the draft, not the delivery: a send an operator approved unchanged
counts whether it went out, was shadowed (nobody sent it, but a person reviewed it, which is
what shadow mode is for) or is still unconfirmed. Auto sends occur only in categories already
on auto, which have no readiness verdict to inflate.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from mappd import db
from mappd.autonomy import Policy, Readiness

from .db import TICKETS
from .schemas import CategoryTrust, TrustReport, Verdict
from .service import CATEGORY, DECISIONS

KIND = TICKETS.c.decision["kind"].astext
# A ticket decided without an agent output has no category of its own.
GROUP = sa.func.coalesce(CATEGORY, "unknown")
# A resolution that closed an operator's escalation is still that proposal, not an agent
# escalation: it counts in proposals (as an edit) and not in the false-escalation rate.
PRIOR_KIND = TICKETS.c.decision["prior"]["kind"].astext
COUNTS = {
    # not IN: mappd.db compiles once, and an expanding IN leaves a POSTCOMPILE placeholder
    "proposals": sa.or_(KIND == "send", KIND == "escalate", PRIOR_KIND == "escalate"),
    "auto": DECISIONS["auto"],
    "without_edits": sa.or_(DECISIONS["auto"], DECISIONS["unedited"]),
    "resolutions": sa.and_(KIND == "resolve", TICKETS.c.decision["prior"].is_(None)),
    "false_escalations": DECISIONS["false_escalation"],
}


def _rate(part: int, whole: int) -> float | None:
    return None if whole == 0 else part / whole


def rates(row: Mapping[str, int], level: str, ready: Readiness, category: str) -> CategoryTrust:
    """One category's numbers and the readiness verdict."""
    edits = _rate(row["without_edits"], row["proposals"])
    verdict: Verdict
    false = _rate(row["false_escalations"], row["resolutions"])
    if level == "auto":
        verdict = "auto"
    elif row["proposals"] < ready.min_cases:
        verdict = "too few cases"
    elif (
        edits is not None
        and edits >= ready.without_edits
        and (false is None or false <= ready.false_escalations_max)
    ):
        verdict = "ready for auto"
    else:
        verdict = "keep review"
    command = (
        f"mappd process autonomy support {category} auto" if verdict == "ready for auto" else None
    )
    return CategoryTrust(
        category=category,
        level=level,
        **{k: row[k] for k in COUNTS},
        without_edits_rate=edits,
        false_escalation_rate=false,
        verdict=verdict,
        command=command,
    )


async def report(days: int = 30, policy: Policy | None = None) -> TrustReport:
    """Per-category trust over the last `days` days of decisions."""
    from .workflow import current_policy  # noqa: PLC0415 - avoid a cycle at import

    policy = policy or current_policy()
    since = datetime.now(UTC) - timedelta(days=days)
    decided_at = sa.cast(TICKETS.c.decision["decided_at"].astext, sa.DateTime(timezone=True))
    columns = [sa.func.count().filter(cond).label(name) for name, cond in COUNTS.items()]
    query = (
        sa.select(GROUP.label("category"), *columns)
        .where(TICKETS.c.decision.is_not(None), decided_at >= since)
        .group_by(GROUP)
        .order_by(GROUP)
    )
    async with db.aconnect() as conn:
        rows = await (await db.arun(conn, query)).fetchall()
    cats = [rates(r, policy.level(r["category"]), policy.ready, r["category"]) for r in rows]
    total = {k: sum(getattr(c, k) for c in cats) for k in COUNTS}
    whole = rates(total, "review", policy.ready, "all").model_copy(update={"command": None})
    return TrustReport(days=days, total=whole, categories=cats)
