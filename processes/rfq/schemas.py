"""Input and output types of the RFQ process."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from mappd.channels.email import Attachment, ReplyContext
from mappd.web.http import Operator
from mappd.workflows import Processing

# Where a case is: new → awaiting_approval | escalated → approved → sent | shadowed; resolved.
type Status = Literal[
    "new", "awaiting_approval", "escalated", "approved", "sent", "shadowed", "resolved"
]
# A list filter: one status, or "all" (what the queue's tabs send).
type StatusFilter = Literal[
    "new", "awaiting_approval", "escalated", "approved", "sent", "shadowed", "resolved", "all"
]
type Outcome = Literal["quote", "clarify", "escalate"]
type Team = Literal["presales", "sales"]


class MailInfo(BaseModel):
    """The email side of a request: the mail it came as (if any), and the quote that went out."""

    message_id: str | None = Field(default=None, description="null for API requests")
    references: list[str] = []
    in_reply_to: str | None = None
    to: list[str] = []
    date: datetime | None = None
    attachments: list[Attachment] = []
    follows: str | None = Field(default=None, description="the request this mail answers")
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


class RfqIn(BaseModel):
    """A request for quotation as it arrives; `external_id` is its stable key."""

    external_id: str = Field(description="mail-<hash> (ids.mail_case) or another stable key")
    sender: str
    subject: str
    body: str
    mail: MailInfo | None = None


class CatalogHit(BaseModel):
    """A catalog product as the extractor's tool shows it."""

    sku: str
    name: str
    family: str
    price_eur: Decimal
    lead_days: int


class Extracted(BaseModel):
    """One requested item as the agent read it."""

    requested: str = Field(description="the customer's own words for the item")
    quantity: int | None = Field(description="how many; null when the request does not say")
    sku: str | None = Field(description="the catalog SKU, only when one product clearly fits")
    confidence: Literal["low", "high"]


class Extraction(BaseModel):
    """What the extractor agent returns for a whole request."""

    lines: list[Extracted]
    needed_by_days: int | None = Field(description="days until the customer needs the goods")
    custom_request: bool = Field(description="integrations, custom builds, installation design")
    language: str = Field(description="ISO 639-1 code")


class Line(BaseModel):
    """One line of the quote as the process decided it."""

    requested: str
    quantity: int | None
    sku: str | None
    name: str | None
    unit_price: Decimal | None
    line_total: Decimal | None
    lead_days: int | None
    alternative: str | None = Field(
        description="a same-family SKU that arrives in time; set only when `late`"
    )
    late: bool = Field(description="`lead_days` is beyond `needed_by_days`, alternative or not")


class RfqOutput(BaseModel):
    """The whole decision for one request: what the evals compare with `expected`."""

    lines: list[Line]
    total: Decimal
    currency: Literal["EUR"] = Field(
        default="EUR", description="of every price and total (the catalog's currency)"
    )
    needed_by_days: int | None
    outcome: Outcome
    escalate_to: Team | None
    language: str
    draft: str | None


class LineFix(BaseModel):
    """An operator's line: the product and quantity it should be."""

    sku: str
    quantity: int = Field(gt=0)


class Decision(BaseModel):
    """An operator's approval of a request at a version; `lines` re-prices the quote."""

    version: int
    operator: Operator
    reply: str | None = Field(
        default=None,
        description="the text to send; empty, or the draft unchanged, sends the quote as"
        " (re-)priced. Words may be added, but every figure line of the quote must stay as"
        " drafted: prices change only through lines",
        json_schema_extra={"widget": "textarea"},
    )
    lines: list[LineFix] | None = Field(
        default=None, description="corrected lines; the quote is re-priced from the catalog"
    )


class Escalation(BaseModel):
    """An operator hands an awaiting request to a team instead of sending the quote."""

    version: int
    operator: Operator
    escalate_to: Team = Field(description="the team that must handle it")
    note: str = Field(default="", json_schema_extra={"widget": "textarea"})


class Resolution(BaseModel):
    """An operator closes an escalated request and judges the agent's escalation."""

    version: int
    operator: Operator
    agent_could_handle: bool = Field(
        default=False, description="the agent could have quoted this itself"
    )
    outcome: Outcome = Field(default="escalate", description="what the agent should have done")
    escalate_to: Team | None = Field(default=None, description="the right team, if any")
    note: str = Field(default="", json_schema_extra={"widget": "textarea"})


class Corrections(BaseModel):
    """What an operator changed in the agent's decision; unset means unchanged."""

    outcome: Outcome | None = None
    escalate_to: Team | None = None
    lines: list[LineFix] | None = None


class DecisionRecord(BaseModel):
    """How a request was decided: the `decision` column (as support's)."""

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


class RfqRecord(BaseModel):
    """A stored request as the workflows see it."""

    id: int
    external_id: str
    sender: str
    subject: str
    body: str
    status: str
    version: int
    output: RfqOutput | None = None
    decision: DecisionRecord | None = None
    mail: MailInfo | None = None

    @property
    def rfq_in(self) -> RfqIn:
        """The intake model this stored request was accepted as."""
        return RfqIn(
            external_id=self.external_id,
            sender=self.sender,
            subject=self.subject,
            body=self.body,
            mail=self.mail,
        )


class RfqOut(BaseModel):
    """The request as the API returns it."""

    external_id: str
    sender: str
    subject: str
    body: str
    status: str
    output: RfqOutput | None = Field(
        description="the decision; null while the request is still `new` (being processed)"
    )
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


class RfqAccepted(BaseModel):
    """What POST /requests answers: the stable key and the workflow processing it."""

    external_id: str
    workflow_id: str


class ApprovalResult(BaseModel):
    """What POST /approve answers: "sent" or "shadowed"; `repeated` when it ran before."""

    result: str
    repeated: bool = False


class DecisionResult(BaseModel):
    """What escalate and resolve answer; `repeated` when this decision ran before."""

    result: str
    repeated: bool = False


class RfqPage(BaseModel):
    """One page of requests, newest first; pass `next` as `before` for the following page."""

    items: list[RfqOut]
    next: str | None


class SchemaOut(BaseModel):
    """JSON Schemas for an own front end."""

    output: dict[str, Any]
    decision: dict[str, Any]


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
