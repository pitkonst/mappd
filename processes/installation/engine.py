"""One technician message through the case: the code both the workflow and the simulator run.

`turn` interprets the message, lets `domain.apply` decide the job's next state, and when the
message blocks the job asks the resolver what to check and propose. It knows nothing about DBOS
or Telegram: the workflow passes durable agents and applies the `Turn` in a transaction; the
eval simulator passes plain agents over in-process systems.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC
from typing import Any

from pydantic import BaseModel
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)

from . import domain
from .schemas import Interpretation, JobRecord, Resolution, State

# The output tool's name: Pydantic AI's structured output, not a system call.
OUTPUT_TOOL = "final_result"


class JobView(BaseModel):
    """What a turn knows about the job (also the resolver's run deps)."""

    external_id: str
    state: State
    skus: list[str]
    site: str
    # When the work is due ("2026-10-24 09:00 UTC"); empty when unknown.
    appointment: str = ""
    # The language the technician writes in (ISO 639-1), as the interpreter read this turn.
    language: str = ""
    history: list[str] = []
    # The turn's stable key (the Telegram update_id; the turn number in a simulation).
    turn_key: str = ""


class ToolCall(BaseModel):
    """One system call the resolver made, for the timeline and the evals."""

    tool: str
    arguments: dict[str, Any]
    result: Any


class Turn(BaseModel):
    """What one message did."""

    state: State
    blocked_from: State | None
    blocked_reason: str | None
    reply: str
    event: str | None
    resolution: Resolution | None = None
    tool_calls: list[ToolCall] = []


def view(job: JobRecord, turn_key: str) -> JobView:
    """What a turn knows about a stored job."""
    at = job.appointment_at
    return JobView(
        external_id=job.external_id,
        state=job.state,
        skus=job.skus,
        site=job.site,
        appointment=at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if at else "",
        history=job.history,
        turn_key=turn_key,
    )


type Interpret = Callable[[JobView, State | None, str, str | None], Awaitable[Interpretation]]
type Resolve = Callable[[JobView, str, list[str]], Awaitable[tuple[Resolution, list[ToolCall]]]]


# What the installer hears when the bot could not read their message: a dispatcher reads it.
UNREAD_REPLY = (
    "Sorry, I could not process that message. I have passed it to your dispatcher, who will"
    " answer you here."
)


async def turn(
    job: JobView, text: str, photo: str | None, *, interpret: Interpret, resolve: Resolve
) -> Turn:
    """Apply one message (`photo` is its description, if it had one) to the job.

    An agent that fails never leaves the installer without an answer: the job blocks, so a
    dispatcher sees the message, and the resolution says what went wrong.
    """
    try:
        interp, unread = await interpret(job, domain.next_state(job.state), text, photo), None
    except AgentRunError as e:
        # A real model once ran out of retries on "it is a CTL-H500 here, must be a mistake".
        interp, unread = UNREAD, e
    outcome = domain.apply(job.state, interp, has_photo=photo is not None)
    resolution: Resolution | None = None
    calls: list[ToolCall] = []
    reply = outcome.reply
    if outcome.state == "blocked" and job.state != "blocked":
        reason = outcome.blocked_reason or "other"
        if unread is not None:
            resolution = _not_read(unread)
        else:
            # The resolver must read the message that stopped the job: which part, what broke.
            said = f"installer: {text}" + (f" [photo: {photo}]" if photo else "")
            # The resolver writes its own reply: it is told the language the interpreter read.
            told = job.model_copy(update={"language": interp.language})
            resolution, calls, reply = await _resolve(
                resolve, told, reason, [*job.history, said], outcome.reply
            )
    return Turn(
        state=outcome.state,
        blocked_from=outcome.blocked_from,
        blocked_reason=outcome.blocked_reason,
        reply=reply,
        event=outcome.event,
        resolution=resolution,
        tool_calls=calls,
    )


UNREAD = Interpretation(
    intent="exception", reached_next=False, exception="other", reply=UNREAD_REPLY
)


async def _resolve(
    resolve: Resolve, job: JobView, reason: str, history: list[str], acknowledgement: str
) -> tuple[Resolution, list[ToolCall], str]:
    """The resolution, its system calls and the reply; a resolver that fails still answers."""
    try:
        resolution, calls = await resolve(job, reason, history)
    except AgentRunError as e:
        # It kept calling tools or ran out of retries: the job still blocks, so a dispatcher sees
        # it, and the installer gets the interpreter's acknowledgement instead of silence.
        return _gave_up(reason, e), [], acknowledgement
    if reason == "incident":
        # A hard rule: an incident is for a dispatcher; nothing is reserved or booked.
        resolution = resolution.model_copy(update={"proposals": []})
    return resolution, calls, resolution.reply


def _not_read(error: AgentRunError) -> Resolution:
    """The resolution of a message the interpreter could not read."""
    summary = f"The bot could not read this message ({error}): read it and answer the technician."
    return Resolution(summary=summary, reply="", proposals=[])


def _gave_up(reason: str, error: AgentRunError) -> Resolution:
    """The resolution of a resolver that failed: nothing checked, nothing proposed."""
    summary = (
        f"The resolver could not finish ({error}). Nothing was checked or proposed: "
        f"look at the {reason.replace('_', ' ')} yourself."
    )
    return Resolution(summary=summary, reply="", proposals=[])


def tool_calls(messages: list[ModelMessage]) -> list[ToolCall]:
    """The system calls in an agent run's messages, each with its result."""
    calls: dict[str, ToolCallPart] = {}
    out: list[ToolCall] = []
    for message in messages:
        for part in message.parts:
            if isinstance(message, ModelResponse) and isinstance(part, ToolCallPart):
                if part.tool_name != OUTPUT_TOOL:
                    calls[part.tool_call_id] = part
            elif isinstance(message, ModelRequest) and isinstance(part, ToolReturnPart):
                call = calls.pop(part.tool_call_id, None)
                if call is not None:
                    out.append(
                        ToolCall(
                            tool=call.tool_name, arguments=call.args_as_dict(), result=part.content
                        )
                    )
    return out
