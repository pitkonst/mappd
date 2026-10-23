"""Domain logic of the support process: pure functions with no knowledge of the runtime."""

from datetime import datetime

from mappd import ids

from .schemas import (
    Category,
    Classification,
    Corrections,
    DecisionRecord,
    Escalation,
    Outcome,
    Resolution,
    Role,
    SupportOutput,
)

# Hard rules are code, not prompts (AGENTS.md, rule 6).
ALWAYS_ESCALATE: dict[str, Role] = {"refund": "sales"}
SAFETY_ROLE: Role = "service"
ROLE_BY_ROUTE: dict[str, Role] = {"commercial": "sales", "technical": "service"}

CLARIFY = (
    "\n\nThe answer depends on a fact this ticket leaves out (order number, site or device model)."
    " Do not answer yet: ask the customer for the missing fact in one or two sentences."
)


def decide(classification: Classification) -> tuple[Outcome, Role | None]:
    """The outcome and the team: hard rules first, then the classifier's proposal."""
    if classification.topic in ALWAYS_ESCALATE:
        return "escalate", ALWAYS_ESCALATE[classification.topic]
    if classification.safety_risk:
        return "escalate", SAFETY_ROLE
    if classification.outcome == "escalate":
        return "escalate", classification.escalate_to or ROLE_BY_ROUTE[classification.route]
    return classification.outcome, None


def escalated_by_rule(output: SupportOutput | None) -> str | None:
    """Why a hard rule escalated this decision, or None: then the agent cannot have erred."""
    if output is not None and output.outcome == "escalate" and output.topic in ALWAYS_ESCALATE:
        return f"{output.topic} always goes to {ALWAYS_ESCALATE[output.topic]} (a hard rule)"
    return None


def ticket_text(subject: str, body: str) -> str:
    """The text the agents read."""
    return f"Subject: {subject}\n\n{body}"


LANGUAGES = {
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
    "pt": "Portuguese",
}


def draft_prompt(text: str, outcome: Outcome, language: str) -> str:
    """What the drafter sees; a "clarify" ticket asks for the missing fact, not an answer.

    The classifier's language is named: a short email alone leaves the drafter guessing.
    """
    name = LANGUAGES.get(language)
    lang = f"{name} ({language})" if name else f"the language with the language code {language}"
    prompt = text + CLARIFY if outcome == "clarify" else text
    return f"{prompt}\n\nWrite the reply in {lang}."


def ticket_workflow_id(external_id: str) -> str:
    """The workflow that processes a ticket: one per external_id."""
    return ids.from_key("support.ticket", external_id)


def approval_workflow_id(external_id: str, version: int) -> str:
    """The workflow of one decision: retries and double clicks collapse into it."""
    return ids.decision("support.approve", external_id, version)


def auto_sends(outcome: str, level: str) -> bool:
    """Whether a decision goes out without approval: replies and questions, never escalations."""
    return level == "auto" and outcome in ("reply", "clarify")


def send_record(
    *,
    operator: str,
    shadow: bool,
    reply: str,
    draft: str | None,
    topic: Category | None,
    agent_topic: str | None,
    now: datetime,
) -> DecisionRecord:
    """The record of a send: whether the reply was edited and which category was corrected."""
    corrected = (
        Corrections(topic=topic) if topic is not None and topic != agent_topic else Corrections()
    )
    return DecisionRecord(
        kind="send",
        operator=operator,
        shadow=shadow,
        edited_reply=draft is not None and reply != draft,
        corrected=corrected,
        decided_at=now,
    )


def _topic(topic: Category | None, agent_topic: str | None) -> Category | None:
    """A topic counts as a correction only when it differs from the agent's."""
    return topic if topic is not None and topic != agent_topic else None


def escalation_record(e: Escalation, *, agent_topic: str | None, now: datetime) -> DecisionRecord:
    """An operator escalated what the agent wanted to answer: the outcome was wrong."""
    return DecisionRecord(
        kind="escalate",
        operator=e.operator,
        corrected=Corrections(
            topic=_topic(e.topic, agent_topic), outcome="escalate", escalate_to=e.escalate_to
        ),
        note=e.note or None,
        decided_at=now,
    )


def resolution_record(
    r: Resolution,
    *,
    agent: SupportOutput | None,
    now: datetime,
    prior: DecisionRecord | None = None,
) -> DecisionRecord:
    """An operator closed an escalation and judged it; only real differences are corrections.

    `prior` is the operator escalation being closed, kept so the trust numbers and the
    feedback case still see it (an agent escalation has none).
    """
    escalated_to = prior.corrected.escalate_to if prior else None
    current_team = escalated_to or (agent.escalate_to if agent else None)
    team = r.escalate_to if r.escalate_to != current_team else None
    return DecisionRecord(
        kind="resolve",
        operator=r.operator,
        agent_could_handle=r.agent_could_handle,
        corrected=Corrections(
            topic=_topic(r.topic, agent.topic if agent else None),
            outcome=r.outcome if r.outcome != "escalate" else None,
            escalate_to=team if r.outcome == "escalate" else None,
        ),
        note=r.note or None,
        decided_at=now,
        prior=prior,
    )


def decision_workflow_id(kind: str, external_id: str, version: int, key: str | None) -> str:
    """A decision's workflow: by its idempotency key when given, else by ticket and version."""
    if key is not None:
        return ids.from_key(kind, f"{external_id}:{key}")
    return ids.decision(kind, external_id, version)
