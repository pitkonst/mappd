"""Dialogue-simulation metrics for installation (`kind: python` in config.yaml).

Each takes the case (the scenario and what should happen) and the simulated episode, and
returns a score in [0, 1], or None when the case is out of the metric's scope.
"""

from collections import Counter
from collections.abc import Mapping
from typing import Any

from mappd.data.cases import Case

from .. import domain

ORDER = {state: i for i, state in enumerate(domain.FLOW)}
# A block may come with the message that reveals the problem or the next one.
LATE_BY = 1


def no_premature_transition(case: Case, output: Mapping[str, Any]) -> float:
    """1 when the job never ran ahead of what had really happened on site."""
    del case
    for got, truth in zip(
        output.get("states") or [], output.get("true_states") or [], strict=False
    ):
        if got in ORDER and truth in ORDER and ORDER[got] > ORDER[truth]:
            return 0.0
    return 1.0


def timely_escalation(case: Case, output: Mapping[str, Any]) -> float:
    """1 when the job blocks within one turn of the problem's reveal, and never without one."""
    blocked = output.get("blocked_turn")
    if case.expected.get("exception") is None:
        return float(blocked is None)
    revealed = output.get("revealed_turn")
    if blocked is None or revealed is None:
        return 0.0
    return float(0 <= blocked - revealed <= LATE_BY)


def _keys(proposals: list[Mapping[str, Any]], *, from_arguments: bool) -> Counter[tuple[str, str]]:
    out: Counter[tuple[str, str]] = Counter()
    for p in proposals:
        tool = str(p.get("tool"))
        args = p.get("arguments") or {} if from_arguments else p
        if tool == "reserve_parts":
            skus = args.get("skus") if from_arguments else [args.get("sku")]
            for sku in skus or []:
                out[(tool, str(sku))] += 1
        elif tool == "book_delivery":
            out[(tool, str(args.get("slot")))] += 1
    return out


def proposals_match(case: Case, output: Mapping[str, Any]) -> float | None:
    """F1 of the proposed commitments by (tool, SKU or slot); None for a job without trouble."""
    if case.expected.get("exception") is None:
        return None
    want = _keys(case.expected.get("proposals") or [], from_arguments=False)
    got = _keys(output.get("proposals") or [], from_arguments=True)
    if not want and not got:
        return 1.0
    hits = sum((want & got).values())
    if hits == 0:
        return 0.0
    precision, recall = hits / sum(got.values()), hits / sum(want.values())
    return 2 * precision * recall / (precision + recall)
