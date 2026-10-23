"""Input and output types of the support process."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from mappd.channels.email import Attachment, ReplyContext
from mappd.web.http import Operator
from mappd.workflows import Processing

EMBED_DIM = 64  # the knowledge-base vector size; real models are asked for this dimension


class MailInfo(BaseModel):
    """The email side of a ticket: the mail it came as (if any), and the reply that went out."""

    message_id: str | None = Field(default=None, description="null for API tickets")
    references: list[str] = []
    in_reply_to: str | None = None
    to: list[str] = []
    date: datetime | None = None
    attachments: list[Attachment] = []
    follows: str | None = Field(default=None, description="the ticket this mail answers")
    sent_message_id: str | None = None

    def reply_context(self, subject: str, sender: str, body: str) -> ReplyContext | None:
        """What a reply to this mail threads under and quotes; None when it came another way."""
        if self.message_id is None:
            return None
        return ReplyContext(
            message_id=self.message_id,
            references=self.references,
            subject=subject,
            sender=sender,
            date=self.date,
            text=body,
        )


class TicketIn(BaseModel):
    """A ticket as it arrives; `external_id` is its stable key."""

    external_id: str = Field(
        description="mail-<hash> (ids.mail_case), tg-<update_id>, or another stable key"
    )
    sender: str
    subject: str
    body: str
    mail: MailInfo | None = None


# Where a case is: new → awaiting_approval | escalated → approved → sent | shadowed; resolved.
type Status = Literal[
    "new", "awaiting_approval", "escalated", "approved", "sent", "shadowed", "resolved"
]
# A list filter: one status, or "all" (what the queue's tabs send).
type StatusFilter = Literal[
    "new", "awaiting_approval", "escalated", "approved", "sent", "shadowed", "resolved", "all"
]
type Role = Literal["sales", "billing", "logistics", "presales", "installation", "service"]
type Outcome = Literal["reply", "clarify", "escalate"]
type Category = Literal["refund", "malfunction", "how_to", "billing", "delivery", "other"]


class Classification(BaseModel):
    """What the classifier agent decides; `domain.decide` applies the hard rules on top."""

    topic: Category
    route: Literal["commercial", "technical"]
    urgency: Literal["low", "normal", "high"]
    language: str = Field(description="ISO 639-1 code")
    safety_risk: bool = Field(description="smoke, fire, sparks, electric shock or anything unsafe")
    outcome: Outcome = Field(
        description="reply if the knowledge base answers it; clarify if the answer depends on an"
        " order, site or device the customer did not name; escalate if a person must handle it"
    )
    escalate_to: Role | None = Field(default=None, description="the team, when escalating")


class Draft(BaseModel):
    """What the drafter agent writes."""

    reply: str
    citations: list[str] = Field(description="IDs of knowledge-base articles the reply relies on")


class SupportOutput(BaseModel):
    """The whole decision for one ticket: what the evals compare with `expected`."""

    topic: str
    route: str
    urgency: str
    language: str
    outcome: Outcome
    escalate_to: Role | None
    draft: Draft | None


class Decision(BaseModel):
    """An operator's approval of a ticket at a version."""

    version: int
    operator: Operator
    reply: str = Field(json_schema_extra={"widget": "textarea"})
    topic: Category | None = Field(
        default=None, description="the category, if the agent got it wrong"
    )


class Escalation(BaseModel):
    """An operator hands an awaiting ticket to a team instead of sending the draft."""

    version: int
    operator: Operator
    escalate_to: Role = Field(description="the team that must handle it")
    topic: Category | None = Field(
        default=None, description="the category, if the agent got it wrong"
    )
    note: str = Field(default="", json_schema_extra={"widget": "textarea"})


class Resolution(BaseModel):
    """An operator closes an escalated ticket and judges the agent's escalation."""

    version: int
    operator: Operator
    agent_could_handle: bool = Field(
        default=False, description="the agent could have answered this itself"
    )
    outcome: Outcome = Field(default="escalate", description="what the agent should have done")
    escalate_to: Role | None = Field(
        default=None, description="the right team, if escalation was right"
    )
    topic: Category | None = Field(
        default=None, description="the category, if the agent got it wrong"
    )
    note: str = Field(default="", json_schema_extra={"widget": "textarea"})


class Corrections(BaseModel):
    """What an operator changed in the agent's decision; unset means unchanged."""

    topic: Category | None = None
    outcome: Outcome | None = None
    escalate_to: Role | None = None


class DecisionRecord(BaseModel):
    """How a ticket was decided: the `decision` column."""

    kind: Literal["send", "escalate", "resolve"]
    operator: str
    shadow: bool = False
    edited_reply: bool = False
    corrected: Corrections = Corrections()
    agent_could_handle: bool | None = None
    note: str | None = None
    decided_at: datetime
    prior: DecisionRecord | None = Field(
        default=None, description="the decision this one closed (an operator's escalation)"
    )

    def stored(self) -> dict[str, Any]:
        """As stored: unset fields dropped, `corrected` always an object (`{}` when none)."""
        data = self.model_dump(mode="json", exclude_none=True, exclude={"prior"})
        data["corrected"] = self.corrected.model_dump(mode="json", exclude_none=True)
        if self.prior is not None:
            data["prior"] = self.prior.stored()
        return data


class TicketRecord(BaseModel):
    """A stored ticket row as the workflows see it."""

    id: int
    external_id: str
    sender: str
    subject: str
    body: str
    status: str
    version: int
    output: SupportOutput | None = None
    decision: DecisionRecord | None = None
    mail: MailInfo | None = None

    @property
    def ticket_in(self) -> TicketIn:
        """The intake model this stored ticket was accepted as."""
        return TicketIn(
            external_id=self.external_id,
            sender=self.sender,
            subject=self.subject,
            body=self.body,
            mail=self.mail,
        )


class TicketOut(BaseModel):
    """The ticket as the API returns it; extra columns of the row are ignored."""

    external_id: str
    sender: str
    subject: str
    body: str
    status: str
    output: SupportOutput | None
    reply: str | None
    approved_by: str | None
    decision: DecisionRecord | None = None
    mail: MailInfo | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    processing: Processing | None = Field(
        default=None,
        description="set while the case is new and its workflow stopped: open it and resume"
        " (cancelled) or retry (failed)",
    )


class TicketAccepted(BaseModel):
    """What POST /tickets answers: the stable key and the workflow processing it."""

    external_id: str
    workflow_id: str


class ApprovalResult(BaseModel):
    """What POST /approve answers: "sent"; `repeated` when this decision ran before."""

    result: str
    repeated: bool = False


class DecisionResult(BaseModel):
    """What escalate and resolve answer; `repeated` when this decision ran before."""

    result: str
    repeated: bool = False


type Verdict = Literal["ready for auto", "keep review", "too few cases", "auto"]


class CategoryTrust(BaseModel):
    """One category's trust numbers over the window."""

    category: str
    level: str
    proposals: int
    auto: int
    without_edits: int
    resolutions: int
    false_escalations: int
    without_edits_rate: float | None
    false_escalation_rate: float | None
    verdict: Verdict
    command: str | None


class TrustReport(BaseModel):
    """Trust per category and in total over the last `days` days."""

    days: int
    total: CategoryTrust
    categories: list[CategoryTrust]


class TicketPage(BaseModel):
    """One page of tickets, newest first; pass `next` as `before` for the following page."""

    items: list[TicketOut]
    next: str | None


class Article(BaseModel):
    """A knowledge-base article as a citation shows it."""

    id: str
    title: str


class SchemaOut(BaseModel):
    """JSON Schemas for an own front end."""

    output: dict[str, Any]
    decision: dict[str, Any]
