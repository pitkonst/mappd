"""Domain logic of the RFQ process: pure functions with no knowledge of the runtime.

The agent reads the request; everything with money or a rule in it happens here (AGENTS.md,
rule 6): prices and lead times from the catalog, totals in Decimal, alternatives for late
lines, the gate, and the draft from a template.
"""

import re
from datetime import datetime
from decimal import Decimal

from mappd import ids

from . import catalog
from .schemas import (
    Corrections,
    DecisionRecord,
    Escalation,
    Extracted,
    Extraction,
    Line,
    LineFix,
    Outcome,
    Resolution,
    RfqOutput,
    Team,
)

CENT = Decimal("0.01")
# What an operator's own words may not carry: a price is the template's alone (rule 6).
MONEY = re.compile(
    r"%|[€$£¥]|\b(?:EUR|USD|GBP|CHF|euros?|dollars?|percent|prozent)\b"
    r"|\d[.,]\d{2}\b",  # an amount like 1102.50
    re.IGNORECASE,
)
DATE = re.compile(r"\b\d{1,2}\.\d{1,2}\.(?:\d{2,4}\b)?")  # 12.11. or 12.11.2026: not an amount
# Volume pricing is a person's call: above this total a quote goes to sales.
ESCALATE_ABOVE = Decimal("10000")


def _line(item: Extracted, needed_by_days: int | None) -> Line:
    product = catalog.PRODUCTS.get(item.sku) if item.sku else None
    quantity = item.quantity if item.quantity is not None and item.quantity > 0 else None
    if product is None:
        return Line(
            requested=item.requested,
            quantity=quantity,
            sku=None,
            name=None,
            unit_price=None,
            line_total=None,
            lead_days=None,
            alternative=None,
            late=False,
        )
    total = (product.price_eur * quantity).quantize(CENT) if quantity is not None else None
    late = needed_by_days is not None and product.lead_days > needed_by_days
    alternative = alternative_for(product.sku, needed_by_days) if late and needed_by_days else None
    return Line(
        requested=item.requested,
        quantity=quantity,
        sku=product.sku,
        name=product.name,
        unit_price=product.price_eur,
        line_total=total,
        lead_days=product.lead_days,
        alternative=alternative,
        late=late,
    )


def price(extraction: Extraction) -> list[Line]:
    """The extracted lines priced from the catalog; unknown SKUs and bad quantities stay open."""
    return [_line(item, extraction.needed_by_days) for item in extraction.lines]


def alternative_for(sku: str, needed_by_days: int) -> str | None:
    """A same-family product arriving within `needed_by_days`, closest in price; None if none."""
    original = catalog.PRODUCTS[sku]
    candidates = [
        p
        for p in catalog.PRODUCTS.values()
        if p.family == original.family and p.sku != sku and p.lead_days <= needed_by_days
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda p: (abs(p.price_eur - original.price_eur), p.sku))
    return best.sku


def total(lines: list[Line]) -> Decimal:
    """The sum of the priced lines, to the cent."""
    return sum((line.line_total or Decimal(0) for line in lines), Decimal(0)).quantize(CENT)


def _unclear(lines: list[Line], extraction: Extraction) -> list[str]:
    """The requested items the customer must make clear: no product, no quantity, a guess."""
    low = {i for i, item in enumerate(extraction.lines) if item.confidence == "low"}
    return [
        line.requested
        for i, line in enumerate(lines)
        if line.sku is None or line.quantity is None or i in low
    ]


def decide(lines: list[Line], extraction: Extraction) -> tuple[Outcome, Team | None]:
    """The gate: custom work to presales, unclear lines to a question, big totals to sales."""
    if extraction.custom_request:
        return "escalate", "presales"
    if not lines or _unclear(lines, extraction):
        return "clarify", None
    if total(lines) > ESCALATE_ABOVE:
        return "escalate", "sales"
    return "quote", None


WORDS = {
    "en": {
        "quote": "Quote",
        "total": "Total",
        "alternative": "faster alternative",
        "late": "late",
        "days": "days",
        "at": "at",
        "clarify": "Before we can quote, please tell us the exact product and quantity for:",
        "vat": "excl. VAT",
    },
    "de": {
        "quote": "Angebot",
        "total": "Gesamt",
        "alternative": "schnellere Alternative",
        "late": "verspätet",
        "days": "Tage",
        "at": "à",
        "clarify": "Bevor wir ein Angebot machen, nennen Sie uns bitte Produkt und Menge für:",
        "vat": "zzgl. MwSt.",
    },
}


def draft(
    outcome: Outcome, lines: list[Line], amount: Decimal, unclear: list[str], language: str
) -> str | None:
    """The quote or the clarifying question, from a template: the figures must be exact."""
    w = WORDS.get(language, WORDS["en"])
    if outcome == "clarify":
        return "\n".join([w["clarify"], *(f"- {item}" for item in unclear)])
    if outcome != "quote":
        return None
    out = [f"{w['quote']}:"]
    for line in lines:
        out.append(
            f"{line.quantity} x {line.name} ({line.sku}) {w['at']} {line.unit_price}"
            f" = {line.line_total} EUR"
        )
        if line.late:
            out.append(f"  {w['late']}: {line.lead_days} {w['days']}")
        if line.alternative:
            alt = catalog.PRODUCTS[line.alternative]
            out.append(f"  {w['alternative']}: {alt.name} ({alt.sku}), {alt.lead_days} {w['days']}")
    out.append(f"{w['total']}: {amount} EUR ({w['vat']})")
    return "\n".join(out)


def solve_output(extraction: Extraction) -> RfqOutput:
    """The decision for an extraction: price, alternatives, gate, draft."""
    lines = price(extraction)
    outcome, team = decide(lines, extraction)
    amount = total(lines)
    return RfqOutput(
        lines=lines,
        total=amount,
        needed_by_days=extraction.needed_by_days,
        outcome=outcome,
        escalate_to=team,
        language=extraction.language,
        draft=draft(outcome, lines, amount, _unclear(lines, extraction), extraction.language),
    )


def escalation_reason(output: RfqOutput | None) -> str | None:
    """Why the gate escalated this request, in words for the operator; None if it did not."""
    if output is None or output.outcome != "escalate":
        return None
    if output.escalate_to == "presales":
        return "the request asks for custom work (integration, custom build): pre-sales scopes it"
    if output.total > ESCALATE_ABOVE:
        return (
            f"the total {output.total} {output.currency} is above {ESCALATE_ABOVE}"
            f" {output.currency}: volume pricing is a person's call"
        )
    return None


def escalated_by_rule(output: RfqOutput | None) -> str | None:
    """The reason when a hard rule (the total threshold) escalated it, or None.

    The agent cannot have erred then. Custom work is the agent's reading, so not a rule here.
    """
    if output is None or output.outcome != "escalate" or output.escalate_to != "sales":
        return None
    return escalation_reason(output)


def rfq_workflow_id(external_id: str) -> str:
    """The workflow that processes a request: one per external_id."""
    return ids.from_key("rfq.request", external_id)


def approval_workflow_id(external_id: str, version: int) -> str:
    """The workflow of one decision: retries and double clicks collapse into it."""
    return ids.decision("rfq.approve", external_id, version)


def auto_sends(outcome: str, level: str) -> bool:
    """Whether a decision goes out without approval: quotes and questions, never escalations."""
    return level == "auto" and outcome in ("quote", "clarify")


def agent_lines(output: RfqOutput | None) -> list[LineFix]:
    """The agent's priced lines as fixes, to compare an operator's correction against."""
    if output is None:
        return []
    return [
        LineFix(sku=line.sku, quantity=line.quantity)
        for line in output.lines
        if line.sku is not None and line.quantity is not None
    ]


def reprice(output: RfqOutput, fixes: list[LineFix]) -> RfqOutput:
    """The quote for an operator's corrected lines: prices, alternatives and draft again."""
    extraction = Extraction(
        lines=[
            Extracted(requested=f.sku, quantity=f.quantity, sku=f.sku, confidence="high")
            for f in fixes
        ],
        needed_by_days=output.needed_by_days,
        custom_request=False,
        language=output.language,
    )
    return solve_output(extraction)


def check_fixes(output: RfqOutput, fixes: list[LineFix]) -> RfqOutput:
    """The quote for an operator's corrected lines, or why it cannot go out as one.

    A correction may only produce a plain quote: unknown products, no lines, or a total the
    threshold sends to sales are refused, so a corrected quote never skips a person's review.

    Raises:
        ValueError: the corrected lines are not a quote that can be sent.
    """
    if not fixes:
        raise ValueError("no lines: a quote needs at least one")
    unknown = [f.sku for f in fixes if f.sku not in catalog.PRODUCTS]
    if unknown:
        raise ValueError(f"not in the catalog: {', '.join(unknown)}")
    priced = reprice(output, fixes)
    if priced.outcome != "quote" or priced.draft is None:
        raise ValueError(
            f"the corrected lines total {priced.total} EUR, above {ESCALATE_ABOVE} EUR:"
            " escalate to sales instead"
            if priced.outcome == "escalate"
            else f"the corrected lines do not make a quote ({priced.outcome})"
        )
    return priced


def figures(output: RfqOutput) -> list[str]:
    """The lines of a quote's draft that carry figures: items, alternatives, lateness, total."""
    if output.outcome != "quote" or output.draft is None:
        return []
    return [line.strip() for line in output.draft.splitlines()[1:] if line.strip()]


def reply_body(agent: RfqOutput | None, priced: RfqOutput | None, reply: str | None) -> str:
    """What goes out: the operator's text, or the draft of `priced` when they left it alone.

    `agent` is the quote the operator saw and `priced` the one that goes out (the same, or
    re-priced from corrected lines). An operator may add words to a quote but never change
    its figures, so the stored output, the API and the webhooks say what the customer read:
    every figure line of `priced` must appear in the text unchanged, and no added line may
    name an amount, a currency or a percentage (rule 6).

    Raises:
        ValueError: nothing to send, the text changes the quote's figures, or adds its own.
    """
    unchanged = reply is None or (agent is not None and reply == agent.draft)
    if unchanged and priced is not None and priced.draft is not None:
        return priced.draft
    if reply is None:
        raise ValueError("no reply: the request has no draft to send")
    kept = {line.strip() for line in reply.splitlines()}
    missing = [line for line in (figures(priced) if priced else []) if line not in kept]
    if missing:
        raise ValueError(
            "the reply changes the quote's figures; correct them through lines, not the text"
            f" (missing: {'; '.join(missing)})"
        )
    drafted = {line.strip() for line in (priced.draft or "").splitlines()} if priced else set()
    priced_words = [
        line.strip()
        for line in reply.splitlines()
        if line.strip() not in drafted and MONEY.search(DATE.sub("", line))
    ]
    if priced_words:
        raise ValueError(
            "the added text names an amount, a currency or a percentage; prices come only from"
            f" the quote's lines (found: {'; '.join(priced_words)})"
        )
    return reply


def send_record(
    *,
    operator: str,
    reply: str | None,
    draft: str | None,
    lines: list[LineFix] | None,
    agent: list[LineFix],
    now: datetime,
) -> DecisionRecord:
    """The record of a send: whether the reply was edited and whether the lines were."""
    changed = lines is not None and lines != agent
    return DecisionRecord(
        kind="send",
        operator=operator,
        edited_reply=reply is not None and draft is not None and reply != draft,
        corrected=Corrections(lines=lines) if changed else Corrections(),
        decided_at=now,
    )


def escalation_record(e: Escalation, *, now: datetime) -> DecisionRecord:
    """An operator escalated what the agent wanted to quote: the outcome was wrong."""
    return DecisionRecord(
        kind="escalate",
        operator=e.operator,
        corrected=Corrections(outcome="escalate", escalate_to=e.escalate_to),
        note=e.note or None,
        decided_at=now,
    )


def resolution_record(
    r: Resolution,
    *,
    agent: RfqOutput | None,
    now: datetime,
    prior: DecisionRecord | None = None,
) -> DecisionRecord:
    """An operator closed an escalation and judged it; only real differences are corrections."""
    escalated_to = prior.corrected.escalate_to if prior else None
    current = escalated_to or (agent.escalate_to if agent else None)
    team = r.escalate_to if r.escalate_to != current else None
    return DecisionRecord(
        kind="resolve",
        operator=r.operator,
        agent_could_handle=r.agent_could_handle,
        corrected=Corrections(
            outcome=r.outcome if r.outcome != "escalate" else None,
            escalate_to=team if r.outcome == "escalate" else None,
        ),
        note=r.note or None,
        decided_at=now,
        prior=prior,
    )


def decision_workflow_id(kind: str, external_id: str, version: int, key: str | None) -> str:
    """A decision's workflow: by its idempotency key when given, else by request and version."""
    if key is not None:
        return ids.from_key(kind, f"{external_id}:{key}")
    return ids.decision(kind, external_id, version)


def lines_key(lines: list[LineFix]) -> str:
    """The lines as one comparable string: sorted `SKUxQTY` joined by `;` (the round trip's)."""
    return ";".join(sorted(f"{line.sku}x{line.quantity}" for line in lines))
