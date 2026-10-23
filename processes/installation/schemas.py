"""Types of the installation process."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

type State = Literal[
    "assigned",
    "arrived",
    "access",
    "equipment_found",
    "installed",
    "tested",
    "handed_over",
    "blocked",
]
type ExceptionKind = Literal["missing_parts", "breakage", "no_access", "incident", "other"]


class Interpretation(BaseModel):
    """What the interpreter agent reads in one technician message."""

    intent: Literal["progress", "exception", "question", "chitchat"]
    reached_next: bool = Field(description="the message says the next step is done")
    exception: ExceptionKind | None = Field(
        default=None, description="what went wrong, for an exception"
    )
    reply: str = Field(description="a short reply in the technician's language")
    language: str = Field(
        default="", description="ISO 639-1 code of the language the technician writes in"
    )
    incident: bool = Field(
        default=False,
        description="the message reports violence, threats, an injury, forced entry or another"
        " danger to people, whatever else it says",
    )

    @model_validator(mode="after")
    def _an_exception_names_its_kind(self) -> Interpretation:
        # Without a kind the block's reason is lost; invalid output makes the model try again,
        # and the retry names the real kind. Retries that all fail end in engine.UNREAD.
        if self.intent == "exception" and self.exception is None:
            raise ValueError("exception: name the kind when the intent is exception")
        return self


type ProposalTool = Literal["reserve_parts", "book_delivery"]


class ReserveArgs(BaseModel):
    """What `reserve_parts` reserves (the job comes from the proposal, never from the model)."""

    skus: list[str] = Field(min_length=1)


class BookArgs(BaseModel):
    """Which delivery slot `book_delivery` books."""

    slot: str = Field(min_length=1)


class Proposal(BaseModel):
    """A commitment the resolver proposes; a dispatcher confirms it before it runs."""

    tool: ProposalTool
    arguments: dict[str, Any] = Field(
        description=(
            'reserve_parts: {"skus": [the affected SKUs]}; book_delivery: {"slot": the slot}'
        )
    )
    reason: str = Field(description="why, in one sentence for the dispatcher")

    @model_validator(mode="after")
    def _arguments_fit_the_tool(self) -> Proposal:
        model = ReserveArgs if self.tool == "reserve_parts" else BookArgs
        self.arguments = model.model_validate(self.arguments).model_dump()
        return self


class Resolution(BaseModel):
    """The resolver's answer to a blocked job."""

    summary: str = Field(description="what happened and what was checked, for the dispatcher")
    reply: str = Field(
        description="the message to the technician; promise nothing a dispatcher has not confirmed"
    )
    proposals: list[Proposal] = []


class ProposalOut(BaseModel):
    """A stored proposal as the API returns it."""

    id: str
    tool: ProposalTool
    arguments: dict[str, Any]
    reason: str
    status: Literal["pending", "confirmed", "rejected", "withdrawn", "done", "failed"]
    decided_by: str | None
    result: dict[str, Any] | None


class TimelineEntry(BaseModel):
    """One row of a job's timeline: a message, or an event (a change, a tool call, a decision)."""

    at: datetime
    kind: Literal["message", "event"]
    # installer | bot | agent | dispatcher:<name> | system
    actor: str
    text: str | None = None
    photo: str | None = None
    photo_description: str | None = None
    event: str | None = None
    data: dict[str, Any] = {}


class JobIn(BaseModel):
    """A dispatcher's new job."""

    external_id: str
    installer_name: str = Field(min_length=1)
    site: str
    skus: list[str]
    appointment_at: datetime


class Message(BaseModel):
    """One message of a job's conversation."""

    direction: Literal["in", "out"]
    # installer | bot | dispatcher:<name>
    author: str
    text: str | None
    photo_description: str | None
    created_at: datetime


class JobOut(BaseModel):
    """A job as the API returns it."""

    external_id: str
    installer_name: str
    site: str
    skus: list[str]
    appointment_at: datetime
    state: State
    blocked_from: State | None
    blocked_reason: str | None
    version: int
    deep_link: str | None = None
    overdue: list[str] = []
    taken_over_by: str | None = None
    proposals: list[ProposalOut] = []
    messages: list[Message] = []


class JobRecord(BaseModel):
    """A stored job as the workflows see it."""

    id: int
    external_id: str
    installer_id: int
    site: str
    skus: list[str]
    state: State
    blocked_from: State | None
    version: int
    taken_over_by: str | None = None
    appointment_at: datetime | None = None
    # The latest messages as "author: text", oldest first (the resolver reads them).
    history: list[str] = []
