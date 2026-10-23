"""Domain rules of the installation process: pure, no runtime (AGENTS.md, rule 6).

The case is a state machine in code. The agent only reads a message; whether the job moves,
needs evidence or is blocked is decided here.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from mappd import ids

from .schemas import Interpretation, State

FLOW: tuple[State, ...] = (
    "assigned",
    "arrived",
    "access",
    "equipment_found",
    "installed",
    "tested",
    "handed_over",
)
EVIDENCE: frozenset[State] = frozenset({"installed", "tested"})
ARRIVAL_GRACE = timedelta(minutes=30)
RESPONSE_TIME = timedelta(hours=2)
CLOSED_REPLY = "This job is closed. Thank you!"
BLOCKED_REPLY = "A dispatcher is on it and will get back to you."


@dataclass(frozen=True)
class Outcome:
    """What a message does to a job."""

    state: State
    blocked_from: State | None
    blocked_reason: str | None
    reply: str
    event: str | None


def next_state(state: State) -> State | None:
    """The step after `state` on the forward path; None at the end or when blocked."""
    if state not in FLOW or state == FLOW[-1]:
        return None
    return FLOW[FLOW.index(state) + 1]


def _stopped(
    state: State, nxt: State | None, interp: Interpretation, *, has_photo: bool
) -> Outcome | None:
    """The block a message causes, or None: an incident or a reported problem."""
    if interp.incident:
        # A hard rule: whatever the message also reports, the job stops for a dispatcher.
        return Outcome("blocked", state, "incident", interp.reply, "blocked")
    if interp.intent == "exception":
        # "I arrived, the gate is closed": the step is done, then the job stops after it. A step
        # that needs evidence still needs its photo.
        done = interp.reached_next and nxt is not None and (nxt not in EVIDENCE or has_photo)
        reason = interp.exception or "other"
        return Outcome("blocked", nxt if done else state, reason, interp.reply, "blocked")
    return None


def apply(state: State, interp: Interpretation, *, has_photo: bool) -> Outcome:
    """The job after one interpreted message: one step forward at most, evidence required."""
    if state == "handed_over":
        return Outcome(state, None, None, CLOSED_REPLY, None)
    if state == "blocked":
        return Outcome(state, None, None, BLOCKED_REPLY, None)
    nxt = next_state(state)
    if (stop := _stopped(state, nxt, interp, has_photo=has_photo)) is not None:
        return stop
    if interp.intent == "progress" and interp.reached_next and nxt is not None:
        if nxt in EVIDENCE and not has_photo:
            label = nxt.replace("_", " ")
            return Outcome(state, None, None, f"Please send a photo showing it is {label}.", None)
        return Outcome(
            nxt, None, None, interp.reply, "handed_over" if nxt == "handed_over" else "progress"
        )
    return Outcome(state, None, None, interp.reply, None)


def advance(state: State) -> State:
    """The step a dispatcher moves the job to by hand (no evidence needed).

    Raises:
        ValueError: the job is blocked or already handed over.
    """
    nxt = next_state(state)
    if state == "blocked" or nxt is None:
        raise ValueError(f"cannot advance a job that is {state}")
    return nxt


def unblock(state: State, blocked_from: State | None) -> State:
    """The state a dispatcher's unblock returns the job to.

    Raises:
        ValueError: the job is not blocked.
    """
    if state != "blocked" or blocked_from is None:
        raise ValueError("the job is not blocked")
    return blocked_from


def arrival_due(appointment_at: datetime) -> datetime:
    """When a missing arrival becomes overdue."""
    return appointment_at + ARRIVAL_GRACE


def response_due(now: datetime) -> datetime:
    """When a dispatcher's missing answer to a blocked job becomes overdue."""
    return now + RESPONSE_TIME


def update_workflow_id(update_id: int) -> str:
    """The workflow of one Telegram update: a redelivery collapses into it."""
    return ids.from_key("installation.update", str(update_id))


def deep_link(bot: str, token: str) -> str:
    """The link that opens the bot and sends `/start <token>`."""
    return f"https://t.me/{bot}?start={token}"
