"""Surface text for a case plan, written by the gateway model `synth`.

The prompt carries facts, persona, language, tone and a typo rate, never the labels: the writer
cannot leak the answer into the text.
"""

from typing import TYPE_CHECKING

from mappd.data.cases import CasePlan, Message

if TYPE_CHECKING:
    from mappd.data.llm import Gateway

SYNTH_MODEL = "synth"
LANGUAGES = {"en": "English", "de": "German"}
# Writers asked for 'Subject:' sometimes translate the label into the email's language.
SUBJECT_LABELS = ("subject:", "betreff:")
SYSTEM = (
    "You write realistic customer emails to Helio Building Systems, a smart-building equipment"
    " distributor and installer. Write as the given persona, in the given language and tone."
    " Include every fact; invent no other facts. Answer with 'Subject: <subject>' on the first"
    " line, an empty line, then the email body."
)


def prompt(plan: CasePlan, system: str = SYSTEM) -> list[dict[str, str]]:
    """The writer's messages for one plan."""
    lines: list[str] = []
    if plan.customer is not None:
        persona = plan.customer.persona
        typos = round(persona.typo_rate * 100, 1)
        lines.extend(
            [
                f"Persona: {persona.style}, writing for {plan.customer.company}.",
                f"Sign as: {plan.customer.contact}.",
                f"Language: {LANGUAGES.get(plan.customer.language, plan.customer.language)}.",
                f"Tone: {persona.tone}.",
                f"Typos: about {typos} per 100 words.",
            ]
        )
    lines.extend(["Facts:", *(f"- {fact}" for fact in plan.facts)])
    if plan.omit:
        lines.append(f"Do not mention: {'; '.join(plan.omit)}.")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]


def parse(text: str) -> Message:
    """Split a completion into subject and body."""
    first, _, rest = text.strip().partition("\n")
    if first.lower().startswith(SUBJECT_LABELS):
        return Message(subject=first.split(":", 1)[1].strip(), body=rest.strip())
    return Message(subject="", body=text.strip())


def write(gateway: Gateway, plan: CasePlan, seed: int, system: str = SYSTEM) -> Message:
    """The surface message for `plan`."""
    return parse(gateway.complete(SYNTH_MODEL, prompt(plan, system=system), seed))
