"""The case format shared by synthetic, imported, dev and holdout sets (one JSON per line)."""

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from mappd.data.expand import Customer

if TYPE_CHECKING:
    from pathlib import Path


class Slot(BaseModel):
    """One planned case: its outcome kind and an optional trap."""

    index: int
    kind: str
    trap: str | None = None


class Message(BaseModel):
    """Surface text as the writer produced it."""

    subject: str
    body: str


class CasePlan(BaseModel):
    """What one case must convey, and its ground truth; the writer never sees `labels`."""

    id: str
    slot: Slot
    customer: Customer | None = None
    facts: list[str]
    labels: dict[str, Any]
    # What the email must leave out (a clarify case's missing fact): an instruction, never a fact.
    omit: list[str] = []


class Case(BaseModel):
    """A labelled case: the process input, the expected labels, and how it was made."""

    id: str
    input: dict[str, Any]
    expected: dict[str, Any]
    meta: dict[str, Any]


def write_cases(path: Path, cases: list[Case]) -> None:
    """Write cases as JSONL, creating the directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as out:
        for case in cases:
            out.write(case.model_dump_json() + "\n")


class CaseFormatError(ValueError):
    """A line of a case file is not a case; the message never repeats its contents."""


def read_cases(path: Path) -> list[Case]:
    """Read a JSONL case file.

    Raises:
        CaseFormatError: a line is not a valid case (names the line and the fields, not values).
    """
    cases: list[Case] = []
    with path.open(encoding="utf-8") as lines:
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                cases.append(Case.model_validate_json(line))
            except ValidationError as e:
                fields = ", ".join(".".join(map(str, err["loc"])) or "line" for err in e.errors())
                raise CaseFormatError(f"{path}: line {number} is not a case ({fields})") from None
    return cases
