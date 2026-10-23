"""The round trip: another model reads only the text and must recover the labels."""

import json
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from mappd.data.cases import Message
    from mappd.data.llm import Gateway

CHECK_MODEL = "check"
FENCE = re.compile(r"\{.*\}", re.DOTALL)


class Verdict(BaseModel):
    """Whether a case survives the round trip, and which fields disagreed."""

    keep: bool
    disagreements: list[str]


def parse_fields(text: str) -> dict[str, Any]:
    """The JSON object in a completion (fenced or bare); {} if there is none."""
    match = FENCE.search(text)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def extract(
    gateway: Gateway, message: Message, fields: tuple[str, ...], instructions: str, seed: int
) -> dict[str, Any]:
    """The checker's reading of `fields` from the text alone."""
    system = (
        "Read the customer email and return only a JSON object with the keys "
        f"{', '.join(fields)}. {instructions}"
    )
    user = f"Subject: {message.subject}\n\n{message.body}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return parse_fields(gateway.complete(CHECK_MODEL, messages, seed))


def compare(
    labels: dict[str, Any], got: dict[str, Any], fields: tuple[str, ...], strict: tuple[str, ...]
) -> Verdict:
    """Disagreeing fields; a disagreement on a strict field drops the case."""
    disagreements = [
        f for f in fields if str(got.get(f, "")).lower() != str(labels.get(f, "")).lower()
    ]
    return Verdict(keep=not any(f in strict for f in disagreements), disagreements=disagreements)
