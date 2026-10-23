"""Synthetic scenarios for the installation evals (`mappd data synth installation`).

The same flow as support's and RFQ's: slots by kind and trap, a deterministic plan with the
ground truth, a writer that sees only facts, and a checker that must recover the exception kind
from the writer's text alone.

A plan is a job at a world site, what the systems know (stock, delivery slots, site contacts, a
damaged unit), and the script: the true order of events on site, one numbered step each. The
writer turns each step into the message a technician (one of a synthetic crew, each with a
fixed style) types at that moment. The expected proposals follow the resolver's rules: a part in
stock is reserved, one out of stock is delivered in the site's first free slot; breakage also
opens a ticket; no access proposes nothing.
"""

import random
import re
from pathlib import Path
from typing import Any

from mappd.data.cases import CasePlan, Message, Slot
from mappd.data.expand import Customer, Persona
from mappd.data.hook import SynthSpec
from mappd.data.world import load

from .. import domain
from ..mocks.systems import Facts
from ..sim import Scenario

__all__ = ["FLOW", "CasePlan", "plan_case", "spec", "to_input"]

WORLD = load()
FACTS = Facts.from_world(WORLD)
PRODUCTS = {p.sku: p.name for p in WORLD.catalog}
# SKUs some depot stocks: the ones an installation job carries.
SKUS = sorted({sku for depot in WORLD.depots for sku in depot.stock})
FLOW: tuple[str, ...] = domain.FLOW[1:]  # arrived ... handed_over: what a technician reports

MIX = {"happy": 0.35, "missing_parts": 0.25, "breakage": 0.15, "no_access": 0.15, "incident": 0.1}
TRAPS = {
    "photo_late": "happy",
    "chatty_detour": "happy",
    "missing_but_in_stock": "missing_parts",
    "missing_none_in_stock": "missing_parts",
    # One message that arrives and hits the problem: "I arrived, the gate is locked".
    "arrival_with_problem": "no_access",
}
TRAP_RATE = 0.2
IN_STOCK_SHARE = 0.6
ROUNDTRIP_FIELDS = ("exception_kind",)
STRICT_FIELDS = ROUNDTRIP_FIELDS

_NAMES = (
    "Lena Brandt", "Tomasz Kowal", "Aylin Kaya", "Jonas Richter", "Mira Schulz", "Pavel Novak",
    "Sofia Russo", "Erik Lund", "Nadia Haddad", "Felix Wagner", "Ines Costa", "Kai Becker",
)  # fmt: skip
_TYPOS = {"non_native": 0.04, "chatty": 0.02}


def _crew() -> tuple[Customer, ...]:
    """Twelve synthetic technicians, each with one world style (a persona per technician)."""
    styles = WORLD.personas
    crew = []
    for i, name in enumerate(_NAMES):
        style = styles[i % len(styles)]
        slug = name.lower().replace(" ", ".")
        crew.append(
            Customer(
                id=f"tech-{i:02d}",
                company="Helio Building Systems",
                contact=name,
                email=f"{slug}@example.com",
                language="en",
                persona=Persona(
                    id=f"{style.id}-{i:02d}",
                    style=style.style,
                    tone="casual",
                    typo_rate=_TYPOS.get(style.id, 0.01),
                ),
            )
        )
    return tuple(crew)


CREW = _crew()

WRITER_INSTRUCTIONS = (
    "You write the Telegram messages an installation technician of Helio Building Systems sends"
    " to the dispatch bot while working on a job. Write as the given persona. Answer with"
    " 'Subject: messages' on the first line, an empty line, then exactly one line per numbered"
    " step, numbered the same way ('1. ...'). Each line is what the technician types at that"
    " moment: report only that step, never a later one, and invent nothing. When a step says a"
    " photo is attached, say so ('photo attached')."
)
CHECK_INSTRUCTIONS = (
    "Read the technician's messages. exception_kind is missing_parts (a delivered part or piece"
    " of equipment is missing), breakage (equipment is broken or damaged), no_access (the"
    " technician cannot get in to do the work), incident (the technician reports violence,"
    " threats, an injury or forcing a way in), or none (no such problem is reported)."
)


def _names(skus: list[str]) -> str:
    return ", ".join(PRODUCTS.get(s, s) for s in skus)


def _fact(event: str, site: Any, skus: list[str], part: str | None) -> str:
    facts = {
        "arrived": f"You arrive at {site.name}.",
        "access": f"You get into the technical room ({site.access})",
        "equipment_found": f"You find the delivered equipment: {_names(skus)}.",
        "installed": "You mount and wire the equipment.",
        "tested": "Everything powers up and the test run passes.",
        "handed_over": "The customer's contact signs off and you hand the site over.",
        "chat": "You make small talk and ask the bot whether anyone brought coffee.",
        "missing_parts": f"The {PRODUCTS.get(part or '', part)} is not among the delivered boxes.",
        "breakage": f"The {PRODUCTS.get(part or '', part)} arrived with a cracked housing.",
        "no_access": "Nobody can let you into the plant room: it is locked and the key is gone.",
        "incident": "The security guard refuses to let you in. You push past the guard and get"
        " inside anyway; the guard stumbles.",
    }
    return facts[event]


_PHOTO = {"yes": " A photo is attached.", "late": " The photo follows in your next message."}


def _script(kind: str, trap: str | None, rng: random.Random) -> list[dict[str, str]]:
    if kind == "no_access":
        events = ["no_access"] if trap == "arrival_with_problem" else ["arrived", "no_access"]
    elif kind == "incident":
        events = ["arrived", "incident"]
    elif kind in {"missing_parts", "breakage"}:
        events = ["arrived", "access", kind]
    else:
        events = list(FLOW)
        if trap == "chatty_detour":
            events.insert(rng.randint(1, 3), "chat")
    photos = {"installed": "late" if trap == "photo_late" else "yes", "tested": "yes"}
    return [{"event": e, "photo": photos.get(e, "no")} for e in events]


def plan_case(slot: Slot, rng: random.Random) -> CasePlan:
    """The ground truth of one scenario: facts for the writer, labels for the evals."""
    tech = rng.choice(CREW)
    site = rng.choice(WORLD.sites)
    skus = rng.sample(SKUS, rng.randint(1, 3))
    facts = FACTS.model_copy(deep=True)
    part = rng.choice(skus) if slot.kind in {"missing_parts", "breakage"} else None
    proposals: list[dict[str, Any]] = []
    if part is not None:
        if slot.trap == "missing_but_in_stock":
            in_stock = True
        elif slot.trap == "missing_none_in_stock":
            in_stock = False
        else:
            in_stock = rng.random() < IN_STOCK_SHARE
        facts.stock[part] = rng.randint(1, 3) if in_stock else 0
        if in_stock:
            proposals.append({"tool": "reserve_parts", "sku": part})
        else:
            free = [s.at for s in facts.slots if s.site in {site.name, None}]
            if free:
                proposals.append({"tool": "book_delivery", "slot": free[0]})
    if slot.kind == "breakage" and part is not None:
        facts.damaged = [part]
    script = _script(slot.kind, slot.trap, rng)
    for step in script:
        step["fact"] = _fact(step["event"], site, skus, part) + _PHOTO.get(step["photo"], "")
    if slot.trap == "arrival_with_problem":
        script[0]["fact"] = (
            f"You arrive at {site.name}, but the gate is locked and nobody answers the intercom."
        )
    exception = None if slot.kind == "happy" else slot.kind
    ext = f"i-{slot.index:04d}"
    labels = {
        "job": {"external_id": ext, "site": site.name, "skus": skus},
        "facts": facts.model_dump(mode="json"),
        "script": script,
        "missing_sku": part,
        "final_state": "handed_over" if exception is None else "blocked",
        "exception": exception,
        "exception_kind": exception or "none",
        "proposals": proposals,
        "ticket": slot.kind == "breakage",
    }
    writer_facts = [
        f"You work at {site.name} on a job with: {_names(skus)}.",
        "Your steps, in order:",
        *(f"{i}. {step['fact']}" for i, step in enumerate(script, start=1)),
    ]
    return CasePlan(id=ext, slot=slot, customer=tech, facts=writer_facts, labels=labels)


_LINE = re.compile(r"^\s*(\d+)[.)]\s*(.+?)\s*$")


def to_input(plan: CasePlan, message: Message) -> dict[str, Any]:
    """The scenario: each numbered line on its step (a missing line falls back to the fact)."""
    said: dict[int, str] = {}
    for raw in message.body.splitlines():
        if match := _LINE.match(raw):
            said.setdefault(int(match.group(1)), match.group(2))
    labels = plan.labels
    script = [
        {**step, "line": said.get(i, step["fact"])}
        for i, step in enumerate(labels["script"], start=1)
    ]
    persona = plan.customer.persona.style if plan.customer is not None else ""
    return Scenario.model_validate(
        {"job": labels["job"], "persona": persona, "facts": labels["facts"], "script": script}
    ).model_dump(mode="json")


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
    input_model=Scenario,
    output_dir=Path(__file__).parents[1] / "evals",
)
