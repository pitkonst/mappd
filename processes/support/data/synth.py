"""Synthetic dataset generator for the support process (`mappd data synth support`).

How `mappd data synth` works (inverse generation with round-trip verification):
1. Slot allocation (`MIX`, `TRAPS`, `TRAP_RATE`):
   Splits the requested `n` cases across outcome buckets (`reply`, `clarify`, `escalate`) and
   assigns edge-case traps to a fraction (`TRAP_RATE`) of those slots.
2. Deterministic planning (`plan_case(slot, rng) -> CasePlan`):
   Pure Python code picks ground-truth `labels` first and builds a list of `facts` (what the
   message must state) and `omit` rules (what the message must leave out).
3. Blind surface writing (`WRITER_INSTRUCTIONS`, model `synth`):
   An LLM writes a realistic message from `facts` and `omit` without ever seeing `labels`, so it
   cannot leak the answer into the text.
4. Round-trip verification (`CHECK_INSTRUCTIONS`, `ROUNDTRIP_FIELDS`, `STRICT_FIELDS`, model
   `check`):
   A second LLM reads only the generated message and predicts `ROUNDTRIP_FIELDS`. Any case where
   the checker disagrees with `labels` on `STRICT_FIELDS` is dropped as ambiguous; surviving cases
   are formatted into process inputs via `to_input(plan, message)` and written to `synth.jsonl`.
"""

import random
from pathlib import Path
from typing import Any

from mappd.data.cases import CasePlan, Message, Slot
from mappd.data.expand import Customer, Expansion, expand
from mappd.data.hook import SynthSpec
from mappd.data.world import Article, Product, World, load

from ..schemas import TicketIn

# --- 1. Distribution, Traps, and Round-Trip Prompts --------------------------

WORLD = load()
EXPANSION = expand(WORLD, 42)
INPUT_MODEL = TicketIn  # schema `mappd data import support` validates imported rows against

# Target share of each outcome in the generated dataset (must sum to 1.0).
MIX = {"reply": 0.5, "clarify": 0.2, "escalate": 0.3}

# Edge-case traps and the outcome bucket each trap belongs to.
TRAPS = {
    "refund_without_word": "escalate",
    "passing_refund": "reply",
    "german_commercial": "reply",
    "two_questions": "reply",
    "near_miss_kb": "escalate",
}
TRAP_RATE = 0.15

# Fields extracted by the round-trip checker model (`check`), and the subset that must match
# `plan.labels` exactly for a generated case to be kept.
ROUNDTRIP_FIELDS = ("topic", "route", "urgency", "language", "outcome")
STRICT_FIELDS = ("route", "outcome", "language")

OMISSIONS = {
    "order": "the order number",
    "site": "which site or building",
    "device": "which device or model",
}
ESCALATION_REASONS = {
    "refund": "refunds, including returning goods for their money back",
    "complaint": "complaints",
    "outside_kb": "custom quotes and integrations with other systems",
    "safety": "safety faults",
}
ESCALATIONS = tuple(ESCALATION_REASONS)
MODEL_DEPENDENT = [a.question for a in WORLD.knowledge_base if "device" in a.needs]

# System prompt for the `synth` writer model (receives persona + facts + omissions, never labels).
WRITER_INSTRUCTIONS = (
    "You write realistic customer emails to Helio Building Systems, a smart-building equipment"
    " distributor and installer. Write as the given persona, in the given language and tone."
    " Include every fact; invent no other facts. Answer with 'Subject: <subject>' on the first"
    " line, an empty line, then the email body."
)

# Instructions for the `check` model that recovers `ROUNDTRIP_FIELDS` from the written email alone.
CHECK_INSTRUCTIONS = (
    "Allowed values: route is commercial (money, invoices, deliveries, warranty, quotes, refunds,"
    " complaints) or technical (devices and the app); outcome is reply (the documentation can"
    " answer), clarify (the answer depends on a fact the email leaves out: "
    f"{', '.join(OMISSIONS.values())}; the answers to these questions differ by model, so asked"
    f" without the exact model they need a clarify: {' / '.join(MODEL_DEPENDENT)}) or escalate (a"
    f" person must handle it: {', '.join(ESCALATION_REASONS.values())}, anything the"
    " documentation does not cover; whether a device works with another system is an"
    " integration question and escalates, it is not a clarify);"
    " topic is refund, malfunction, how_to, billing, delivery or other; urgency is low, normal or"
    " high; language is the ISO 639-1 code of the email."
)


# --- 2. Public Planner and Input Formatter (`SynthSpec` callbacks) -----------


def plan_case(slot: Slot, rng: random.Random) -> CasePlan:
    """Build the ground-truth `CasePlan` (facts, omissions, and expected labels) for one `slot`."""
    customers = EXPANSION.customers
    if slot.trap == "german_commercial":
        customers = [c for c in customers if c.language == "de"] or customers
    customer = rng.choice(customers)
    if slot.trap == "german_commercial" and customer.language != "de":
        customer = customer.model_copy(update={"language": "de"})
    if slot.kind == "reply":
        plan = _reply(slot, WORLD, EXPANSION, rng, customer)
    elif slot.kind == "clarify":
        plan = _clarify(slot, WORLD, rng, customer)
    else:
        plan = _escalate(slot, WORLD, EXPANSION, rng, customer)
    return plan.model_copy(update={"id": f"s-{slot.index:04d}"})


def to_input(plan: CasePlan, message: Message) -> dict[str, Any]:
    """Convert a verified `CasePlan` and writer `Message` into a `TicketIn` dictionary."""
    sender = plan.customer.email if plan.customer is not None else "customer@example.com"
    return {
        "external_id": plan.id,
        "sender": sender,
        "subject": message.subject or "Support request",
        "body": message.body,
    }


# --- 3. Outcome-Specific Planners (`reply`, `clarify`, `escalate`) -----------


def _reply(
    slot: Slot, world: World, expansion: Expansion, rng: random.Random, customer: Customer
) -> CasePlan:
    part = "commercial" if slot.trap == "german_commercial" else None
    article = rng.choice(world.articles(part=part, exclude_topics=("refund",)))
    device = rng.choice(world.products_in(article)) if "device" in article.needs else None
    facts = [
        f"The customer asks: {article.question}",
        *_context(customer, expansion, world, rng, device),
    ]
    if slot.trap == "passing_refund":
        facts.append("They mention they once got a refund for another item, but that is settled.")
    if slot.trap == "two_questions":
        facts.append("They also ask whether the office is open on public holidays.")
    labels = _labels(
        customer,
        topic=article.topic,
        route=_route(article),
        outcome="reply",
        citations=[article.id],
    )
    return CasePlan(id="", slot=slot, customer=customer, facts=facts, labels=labels)


def _clarify(slot: Slot, world: World, rng: random.Random, customer: Customer) -> CasePlan:
    """A question whose answer depends on a fact the email leaves out."""
    article = rng.choice([a for a in world.articles(exclude_topics=("refund",)) if a.needs])
    facts = [f"The customer asks: {article.question}"]
    labels = _labels(customer, topic=article.topic, route=_route(article), outcome="clarify")
    omit = [OMISSIONS[need] for need in article.needs]
    return CasePlan(id="", slot=slot, customer=customer, facts=facts, labels=labels, omit=omit)


def _escalate(
    slot: Slot, world: World, expansion: Expansion, rng: random.Random, customer: Customer
) -> CasePlan:
    order = rng.choice(expansion.orders_of(customer.id))
    product = next(p for p in world.catalog if p.sku == order.skus[0])
    site = next(s for s in expansion.sites if s.id == order.site_id)
    omit: list[str] = []
    if slot.trap == "refund_without_word":
        omit = ['the word "refund" or any translation of it (such as "Rückerstattung")']
        facts = [
            f"The customer wants to send the unused {product.name} back and get what they paid"
            f" for order {order.id} returned to them."
        ]
        labels = _labels(
            customer, topic="refund", route="commercial", outcome="escalate", escalate_to="sales"
        )
    elif slot.trap == "near_miss_kb":
        facts = [
            f"The customer asks whether the {product.name} can talk to their building's"
            " elevator system."
        ]
        labels = _labels(
            customer, topic="how_to", route="technical", outcome="escalate", escalate_to="presales"
        )
    else:
        kind = rng.choice(ESCALATIONS)
        if kind == "refund":
            facts = [
                f"The customer wants a refund for the {product.name} from order {order.id};"
                " it was never used."
            ]
            labels = _labels(
                customer,
                topic="refund",
                route="commercial",
                outcome="escalate",
                escalate_to="sales",
            )
        elif kind == "complaint":
            facts = [
                "The customer complains that technicians missed two appointments at"
                f" {site.building} and asks for a manager."
            ]
            labels = _labels(
                customer, topic="other", route="commercial", outcome="escalate", escalate_to="sales"
            )
        elif kind == "outside_kb":
            count = rng.choice((25, 60, 120))
            facts = [
                f"The customer asks for a custom quote for {count} units of {product.name}"
                f" for a new building in {site.city}."
            ]
            labels = _labels(
                customer,
                topic="other",
                route="commercial",
                outcome="escalate",
                escalate_to="presales",
            )
        else:
            facts = [
                f"The customer reports smoke from the {product.name} at {site.building};"
                " they say it is unsafe and they switched it off."
            ]
            labels = _labels(
                customer,
                topic="malfunction",
                route="technical",
                outcome="escalate",
                urgency="high",
                escalate_to="service",
            )
    return CasePlan(id="", slot=slot, customer=customer, facts=facts, labels=labels, omit=omit)


# --- 4. Context and Label Helpers --------------------------------------------


def _context(
    customer: Customer,
    expansion: Expansion,
    world: World,
    rng: random.Random,
    product: Product | None = None,
) -> list[str]:
    order = rng.choice(expansion.orders_of(customer.id))
    site = next(s for s in expansion.sites if s.id == order.site_id)
    product = product or next(p for p in world.catalog if p.sku == order.skus[0])
    return [f"They have a {product.name} at {site.building}, {site.city} (order {order.id})."]


def _route(article: Article) -> str:
    return "commercial" if article.part == "commercial" else "technical"


def _labels(
    customer: Customer,
    *,
    topic: str,
    route: str,
    outcome: str,
    urgency: str = "normal",
    escalate_to: str | None = None,
    citations: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "topic": topic,
        "route": route,
        "urgency": urgency,
        "language": customer.language,
        "outcome": outcome,
        "escalate_to": escalate_to,
        "citations": citations or [],
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
