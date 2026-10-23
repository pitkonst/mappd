"""Synthetic dataset generator for the RFQ process (`mappd data synth rfq`).

The same flow as support's (see processes/support/data/synth.py): slots by outcome and trap,
a deterministic plan with ground-truth labels, a writer that sees only facts, and a checker
that must recover `outcome`, `language` and `lines_key` from the text alone.

Labels are computed by the process's own domain code (prices, totals, alternatives, the gate),
so the expected quote is exactly what correct extraction would produce. Facts never carry a
SKU, a price or a total: the writer describes products in one of three registers — the exact
name, the model code with the family, or what the product is for.
"""

import random
from decimal import Decimal
from pathlib import Path
from typing import Any

from mappd.data.cases import CasePlan, Message, Slot
from mappd.data.expand import Customer, expand
from mappd.data.hook import SynthSpec
from mappd.data.world import load

from .. import catalog, domain
from ..schemas import CatalogHit, Extracted, Extraction, RfqIn

WORLD = load()
EXPANSION = expand(WORLD, 42)
INPUT_MODEL = RfqIn

MIX = {"quote": 0.55, "clarify": 0.2, "escalate": 0.25}
TRAPS = {
    "quantity_in_words": "quote",
    "just_under_threshold": "quote",
    "just_over_threshold": "escalate",
    "german": "quote",
    "old_name": "quote",
    "late_no_alternative": "quote",
}
TRAP_RATE = 0.15
ALTERNATIVE_SHARE = 0.3  # quotes whose needed-by date makes one line need an alternative
FASTEST_LEAD = 2  # days: nothing ships faster, so a line must take longer to need one
HALF = 0.5
ROUNDTRIP_FIELDS = ("outcome", "language", "lines_key")
STRICT_FIELDS = ROUNDTRIP_FIELDS

MAX_QUANTITY = {"controller": 4, "cabinet": 3, "camera": 12, "sensor": 60, "accessory": 10}
# What a product is for, only where exactly one catalog product fits the description.
INTENT = {
    "SEN-T200": "sensors measuring both temperature and humidity",
    "SEN-C300": "sensors to measure CO2 in meeting rooms",
    "SEN-W20": "water leak detectors",
    "SEN-S90": "fire detectors",
    "SEN-V12": "vibration sensors for machinery",
    "CAM-PTZ": "cameras they can pan, tilt and zoom remotely",
    "CAM-F1": "360-degree fisheye cameras",
    "CAB-OUT": "a weatherproof outdoor cabinet",
    "ACC-POE8": "8-port PoE switches",
    "ACC-UPS": "small UPS units",
    "ACC-LIC": "remote access licences",
    "CTL-GW1": "a BACnet gateway",
}
WORDS = {2: "a pair of", 12: "a dozen", 24: "two dozen", 100: "a hundred"}
OLD_NAMES = {"CTL-H400": "Hub 4", "CAM-D2": "Dome 2", "SEN-M110": "PIR-110", "CAB-W60": "Rack 60"}
CUSTOM = (
    "They also need everything integrated with their existing building management system and"
    " commissioned on site by your engineers."
)

WRITER_INSTRUCTIONS = (
    "You write realistic emails in which a customer asks Helio Building Systems, a smart-building"
    " equipment distributor, for a quote. Write as the given persona, in the given language and"
    " tone. List every requested item as described in the facts, with its quantity as given"
    " (keep words like 'a dozen' as words); invent no other items, numbers or product codes."
    " Answer with 'Subject: <subject>' on the first line, an empty line, then the email body."
)
CATALOG_LINES = "; ".join(
    f"{p.sku} = {p.name}, {p.price_eur} EUR" for p in catalog.PRODUCTS.values()
)
CHECK_INSTRUCTIONS = (
    "outcome is quote (every item and its quantity are clear and nothing custom is asked),"
    " clarify (an item or a quantity is unclear or missing), or escalate (integration or other"
    " custom work is asked, or the order would cost more than 10000 EUR); language is the ISO"
    " 639-1 code of the email; lines_key lists each requested item as SKUxQTY (for example"
    " SEN-T100x12), using ? for an unknown SKU or quantity, sorted and joined by ';'. Catalog: "
    + CATALOG_LINES
)


def lines_key(lines: list[dict[str, Any]]) -> str:
    """Sorted `SKUxQTY` joined by `;`, with `?` for an unknown SKU or quantity."""
    return ";".join(
        sorted(f"{line.get('sku') or '?'}x{line.get('quantity') or '?'}" for line in lines)
    )


def _describe(hit: CatalogHit, register: str) -> str:
    if register == "intent" and hit.sku in INTENT:
        return INTENT[hit.sku]
    if register == "model":
        return (
            f"{hit.family}s, model {hit.name.split()[-1]}"
            if hit.family != "accessory"
            else hit.name
        )
    return hit.name


def _pick(rng: random.Random, count: int, *, exclude: set[str] | None = None) -> list[CatalogHit]:
    pool = [p for p in catalog.PRODUCTS.values() if p.sku not in (exclude or set())]
    return rng.sample(pool, count)


def _quantity(rng: random.Random, hit: CatalogHit) -> int:
    return rng.randint(1, MAX_QUANTITY[hit.family])


def _decide(
    items: list[tuple[CatalogHit, int | None]],
    needed_by: int | None,
    *,
    custom: bool = False,
    unknown: str | None = None,
) -> dict[str, Any]:
    """Labels from the process's own domain code for these lines."""
    extraction = Extraction(
        lines=[
            Extracted(requested=hit.name, quantity=qty, sku=hit.sku, confidence="high")
            for hit, qty in items
        ]
        + (
            [Extracted(requested=unknown, quantity=None, sku=None, confidence="low")]
            if unknown
            else []
        ),
        needed_by_days=needed_by,
        custom_request=custom,
        language="en",
    )
    out = domain.solve_output(extraction)
    lines = [{"sku": hit.sku, "quantity": qty} for hit, qty in items]
    if unknown:
        lines.append({"sku": None, "quantity": None})  # "some sensors": no product, no number
    return {
        "outcome": out.outcome,
        "escalate_to": out.escalate_to,
        "lines": lines,  # custom work still lists items: the agent extracts them all the same
        "lines_key": lines_key(lines),
        "total": str(out.total),
        "alternatives": {
            line.sku: line.alternative for line in out.lines if line.sku and line.alternative
        },
        "needed_by_days": needed_by,
    }


def _under_threshold(
    rng: random.Random, items: list[tuple[CatalogHit, int | None]]
) -> list[tuple[CatalogHit, int | None]]:
    """Lower quantities until the quote stays at or under the escalation threshold."""
    items = list(items)
    while (
        domain.total(
            domain.price(
                Extraction(
                    lines=[
                        Extracted(requested=h.name, quantity=q, sku=h.sku, confidence="high")
                        for h, q in items
                    ],
                    needed_by_days=None,
                    custom_request=False,
                    language="en",
                )
            )
        )
        > domain.ESCALATE_ABOVE
    ):
        i = max(range(len(items)), key=lambda k: items[k][0].price_eur * (items[k][1] or 0))
        hit, qty = items[i]
        items[i] = (hit, max(1, (qty or 1) // 2))
    return items


def _facts(customer: Customer, lines: list[str], extra: list[str]) -> list[str]:
    return [f"{customer.company} asks for a quote for:", *(f"- {line}" for line in lines), *extra]


def _line_text(hit: CatalogHit, qty: int | None, register: str, words: bool = False) -> str:
    desc = _describe(hit, register)
    if qty is None:
        return f"{desc} (how many is left open)"
    if words and qty in WORDS:
        return f"{WORDS[qty]} {desc}"
    return f"{qty} x {desc}"


def _register(rng: random.Random, hit: CatalogHit) -> str:
    choices = ["exact", "model"] + (["intent"] if hit.sku in INTENT else [])
    return rng.choice(choices)


def _quote(slot: Slot, rng: random.Random, customer: Customer) -> CasePlan:
    extra: list[str] = []
    needed_by: int | None = None
    if slot.trap in ("just_under_threshold", "just_over_threshold"):
        hit = rng.choice(
            [p for p in catalog.PRODUCTS.values() if Decimal(100) <= p.price_eur <= Decimal(1000)]
        )
        base = int(domain.ESCALATE_ABOVE // hit.price_eur)
        items: list[tuple[CatalogHit, int | None]] = [
            (hit, base if slot.trap == "just_under_threshold" else base + 1)
        ]
    elif slot.trap == "late_no_alternative":
        hit = catalog.PRODUCTS[rng.choice(["CAB-F120", "CAB-F180", "CAB-OUT"])]
        items = [(hit, rng.randint(1, 2))]
        needed_by = 1
    else:
        count = rng.randint(1, 4)
        hits = _pick(rng, count)
        if slot.trap == "old_name":
            old = catalog.PRODUCTS[rng.choice(sorted(OLD_NAMES))]
            hits = [old, *(h for h in hits if h.sku != old.sku)][:count]
        if slot.trap == "quantity_in_words":
            sensors = [p for p in catalog.PRODUCTS.values() if p.family == "sensor"]
            hits[0] = rng.choice(sensors)
            items = [
                (hits[0], rng.choice([2, 12, 24])),
                *((h, _quantity(rng, h)) for h in hits[1:]),
            ]
        else:
            items = [(h, _quantity(rng, h)) for h in hits]
        items = _under_threshold(rng, items)
        if slot.trap is None and rng.random() < ALTERNATIVE_SHARE:
            late = [
                h
                for h, _ in items
                if h.lead_days > FASTEST_LEAD and domain.alternative_for(h.sku, h.lead_days - 1)
            ]
            if late:
                needed_by = late[0].lead_days - 1
    exact = slot.trap in ("just_under_threshold", "just_over_threshold", "old_name")
    lines = []
    for i, (hit, qty) in enumerate(items):
        register = "exact" if exact else _register(rng, hit)
        text = _line_text(hit, qty, register, words=slot.trap == "quantity_in_words" and i == 0)
        if slot.trap == "old_name" and i == 0:
            text += f" (they still call it the {OLD_NAMES[hit.sku]} in their notes)"
        lines.append(text)
    if needed_by is not None:
        extra.append(f"They need everything within {needed_by} days.")
    labels = _decide(items, needed_by)
    return CasePlan(
        id="",
        slot=slot,
        customer=customer,
        facts=_facts(customer, lines, extra),
        labels={**labels, "language": customer.language},
    )


def _clarify(slot: Slot, rng: random.Random, customer: Customer) -> CasePlan:
    hits = _pick(rng, rng.randint(2, 3))
    items: list[tuple[CatalogHit, int | None]] = _under_threshold(
        rng, [(h, _quantity(rng, h)) for h in hits]
    )
    lines = [_line_text(h, q, _register(rng, h)) for h, q in items]
    if rng.random() < HALF:  # a missing quantity, else a vague product
        hit, _ = items[0]
        items[0] = (hit, None)
        lines[0] = _describe(hit, "exact")
        omit = [f"how many {hit.name} units they need"]
        labels = _decide(items[1:], None)
        labels["lines"] = [{"sku": hit.sku, "quantity": None}, *labels["lines"]]
        labels["lines_key"] = lines_key(labels["lines"])
        labels["outcome"], labels["escalate_to"] = "clarify", None
        return CasePlan(
            id="",
            slot=slot,
            customer=customer,
            facts=_facts(customer, lines, []),
            labels={**labels, "language": customer.language},
            omit=omit,
        )
    family = rng.choice(["sensor", "camera"])
    vague = f"some {family}s for a new office, model not decided"
    labels = _decide(items, None, unknown=f"{family}s")
    labels["outcome"], labels["escalate_to"] = "clarify", None
    return CasePlan(
        id="",
        slot=slot,
        customer=customer,
        facts=_facts(customer, [*lines, vague], []),
        labels={**labels, "language": customer.language},
    )


def _escalate(slot: Slot, rng: random.Random, customer: Customer) -> CasePlan:
    if slot.trap == "just_over_threshold" or rng.random() < HALF:  # a big order, else custom work
        if slot.trap != "just_over_threshold":
            hit = rng.choice(
                [
                    p
                    for p in catalog.PRODUCTS.values()
                    if Decimal(300) <= p.price_eur <= Decimal(1000)
                ]
            )
            items: list[tuple[CatalogHit, int | None]] = [
                (hit, int(domain.ESCALATE_ABOVE // hit.price_eur) + rng.randint(2, 8))
            ]
            lines = [_line_text(hit, items[0][1], "exact")]
            labels = _decide(items, None)
            return CasePlan(
                id="",
                slot=slot,
                customer=customer,
                facts=_facts(customer, lines, []),
                labels={**labels, "language": customer.language},
            )
        return _quote(slot, rng, customer)
    hits = _pick(rng, rng.randint(1, 3))
    items = [(h, _quantity(rng, h)) for h in hits]
    lines = [_line_text(h, q, "exact") for h, q in items]
    labels = _decide(items, None, custom=True)
    return CasePlan(
        id="",
        slot=slot,
        customer=customer,
        facts=_facts(customer, lines, [CUSTOM]),
        labels={**labels, "language": customer.language},
    )


def plan_case(slot: Slot, rng: random.Random) -> CasePlan:
    """Build the ground-truth plan for one slot: facts for the writer, labels for the evals."""
    customers = EXPANSION.customers
    if slot.trap == "german":
        customers = [c for c in customers if c.language == "de"] or customers
    customer = rng.choice(customers)
    if slot.trap == "german" and customer.language != "de":
        customer = customer.model_copy(update={"language": "de"})
    if slot.kind == "clarify":
        plan = _clarify(slot, rng, customer)
    elif slot.kind == "escalate":
        plan = _escalate(slot, rng, customer)
    else:
        plan = _quote(slot, rng, customer)
    return plan.model_copy(update={"id": f"r-{slot.index:04d}"})


def to_input(plan: CasePlan, message: Message) -> dict[str, Any]:
    """A verified plan and the writer's message as an `RfqIn` dictionary."""
    sender = plan.customer.email if plan.customer is not None else "customer@example.com"
    return {
        "external_id": plan.id,
        "sender": sender,
        "subject": message.subject or "Request for quotation",
        "body": message.body,
    }


spec = SynthSpec(
    mix=MIX,
    traps=TRAPS,
    trap_rate=TRAP_RATE,
    roundtrip_fields=ROUNDTRIP_FIELDS,
    strict_fields=STRICT_FIELDS,
    writer_instructions=WRITER_INSTRUCTIONS,
    check_instructions=CHECK_INSTRUCTIONS,
    plan_case=plan_case,
    to_input=to_input,
    input_model=INPUT_MODEL,
    output_dir=Path(__file__).parents[1] / "evals",
)
