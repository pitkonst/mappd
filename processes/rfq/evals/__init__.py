"""The RFQ process's evaluation specification (`mappd eval run rfq`)."""

import re
from typing import Any

from mappd.eval.hook import EvalSpec

from .. import catalog, domain
from ..schemas import Extracted, Extraction, RfqIn
from ..solve import solve as solve_rfq

NUMBER_THEN_WORDS = re.compile(r"(\d+)\s*(?:x\s*)?([A-Za-z][\w\- ]{2,40})")


async def solve(input: dict[str, Any]) -> dict[str, Any]:  # noqa: A002 - the hook's API
    """The RFQ decision for one case input."""
    output = await solve_rfq(RfqIn.model_validate(input))
    return output.model_dump(mode="json")


def keyword_baseline(input: dict[str, Any]) -> dict[str, Any]:  # noqa: A002 - the hook's API
    """A floor: each "<number> <words>" becomes a line with the best keyword match."""
    text = f"{input.get('subject', '')}\n{input.get('body', '')}"
    lines = []
    for qty, words in NUMBER_THEN_WORDS.findall(text):
        hits = catalog.search(words, limit=1)
        if hits:
            lines.append(
                Extracted(requested=words, quantity=int(qty), sku=hits[0].sku, confidence="high")
            )
    extraction = Extraction(lines=lines, needed_by_days=None, custom_request=False, language="en")
    return domain.solve_output(extraction).model_dump(mode="json")


spec = EvalSpec(
    solve=solve,
    judge_context=lambda _case, _output: None,
    baselines={"keyword": keyword_baseline},
)
