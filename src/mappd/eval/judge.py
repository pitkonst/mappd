"""The LLM judge: the gateway model `judge` scores an output against a rubric and context."""

from typing import TYPE_CHECKING

from pydantic import BaseModel

from mappd.data.roundtrip import parse_fields

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mappd.data.llm import Gateway

JUDGE_MODEL = "judge"
DEFAULT_CRITERIA = ("answers", "grounded", "language", "safe")
CRITERIA = DEFAULT_CRITERIA
ATTEMPTS = 2  # one retry, with another seed, when the answer cannot be read


def _answer_format(criteria: Sequence[str]) -> str:
    keys = ", ".join(f'"{c}": bool' for c in criteria)
    return f'\n\nAnswer with only a JSON object: {{{keys}, "note": "one sentence"}}.'


class Verdict(BaseModel):
    """The judge's reading of one case against the configured boolean criteria."""

    checks: dict[str, bool]
    note: str = ""

    @property
    def passed(self) -> bool:
        """All criteria hold."""
        return bool(self.checks) and all(self.checks.values())


def _parse_verdict(fields: dict[str, object], criteria: Sequence[str]) -> Verdict | None:
    checks: dict[str, bool] = {}
    for name in criteria:
        value = fields.get(name)
        if not isinstance(value, bool):
            return None
        checks[name] = value
    raw_note = fields.get("note", "")
    if not isinstance(raw_note, str):
        return None
    return Verdict(checks=checks, note=raw_note)


def judge(
    gateway: Gateway,
    rubric: str,
    context: Mapping[str, str],
    seed: int = 0,
    criteria: Sequence[str] = DEFAULT_CRITERIA,
) -> Verdict | None:
    """The verdict, or None when the judge's answer cannot be read after one retry."""
    user = "\n\n".join(f"{key.capitalize()}:\n{value}" for key, value in context.items())
    messages = [
        {"role": "system", "content": rubric.strip() + _answer_format(criteria)},
        {"role": "user", "content": user},
    ]
    for attempt in range(ATTEMPTS):
        fields = parse_fields(gateway.complete(JUDGE_MODEL, messages, seed + attempt))
        verdict = _parse_verdict(fields, criteria)
        if verdict is not None:
            return verdict
    return None
