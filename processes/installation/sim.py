"""Simulation scenarios for the installation evals: a job, what the systems know, a script.

A scenario is an eval case's input. The simulated technician follows the `script` — the true
order of events on site — one step per turn, in the persona's voice (`line`, written by the
synth writer); the systems answer from `facts`.
"""

from typing import Literal

from pydantic import BaseModel

from .mocks.systems import Facts
from .schemas import ExceptionKind, State

type StepEvent = Literal[
    "arrived",
    "access",
    "equipment_found",
    "installed",
    "tested",
    "handed_over",
    "chat",
    "missing_parts",
    "breakage",
    "no_access",
    "incident",
]
# Steps that are exceptions: the job should block on them.
EXCEPTIONS: frozenset[str] = frozenset({"missing_parts", "breakage", "no_access", "incident"})


class Step(BaseModel):
    """One thing that happens on site, and what the technician says about it."""

    event: StepEvent
    # yes: the message carries a photo; late: the photo follows in the next message.
    photo: Literal["yes", "no", "late"] = "no"
    line: str
    fact: str = ""


class JobSpec(BaseModel):
    """The job as a dispatcher created it."""

    external_id: str
    site: str
    skus: list[str]


class Scenario(BaseModel):
    """One simulated job (an eval case's input)."""

    job: JobSpec
    persona: str
    facts: Facts
    script: list[Step]


class ExpectedProposal(BaseModel):
    """A commitment the resolver should propose, by its key argument."""

    tool: Literal["reserve_parts", "book_delivery"]
    sku: str | None = None
    slot: str | None = None


class Expected(BaseModel):
    """What should happen (an eval case's expected labels)."""

    final_state: State
    exception: ExceptionKind | None
    proposals: list[ExpectedProposal]
    ticket: bool
