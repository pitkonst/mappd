"""Field-comparison metrics for RFQ (`kind: python` in config.yaml).

Each takes the case and the process output (as JSON) and returns a score in [0, 1], or None
when the case is out of the metric's scope. Lines without a SKU are ignored on both sides: an
unmatched line is scored by the outcome (clarify), not as a line item.
"""

from collections import Counter
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from mappd.data.cases import Case


def _pairs(lines: Any, *, with_quantity: bool = True) -> Counter[tuple[str, Any]]:
    out: Counter[tuple[str, Any]] = Counter()
    for line in lines or []:
        sku = line.get("sku")
        if sku:
            out[(sku, line.get("quantity") if with_quantity else None)] += 1
    return out


def _f1(expected: Counter[tuple[str, Any]], got: Counter[tuple[str, Any]]) -> float:
    if not expected and not got:
        return 1.0
    hits = sum((expected & got).values())
    if hits == 0:
        return 0.0
    precision, recall = hits / sum(got.values()), hits / sum(expected.values())
    return 2 * precision * recall / (precision + recall)


def line_items_f1(case: Case, output: Mapping[str, Any]) -> float | None:
    """F1 over (SKU, quantity) pairs, as a multiset."""
    return _f1(_pairs(case.expected.get("lines")), _pairs(output.get("lines")))


def sku_f1(case: Case, output: Mapping[str, Any]) -> float | None:
    """F1 over SKUs alone: matching, apart from reading quantities."""
    return _f1(
        _pairs(case.expected.get("lines"), with_quantity=False),
        _pairs(output.get("lines"), with_quantity=False),
    )


def _money(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except InvalidOperation, ValueError:
        return None


def totals_check(case: Case, output: Mapping[str, Any]) -> float | None:
    """1 when the quoted total matches the expected one to the cent; quotes only."""
    if case.expected.get("outcome") != "quote":
        return None
    expected, got = _money(case.expected.get("total")), _money(output.get("total"))
    if expected is None or got is None:
        return 0.0
    return float(abs(expected - got) < Decimal("0.01"))


def alternatives_accuracy(case: Case, output: Mapping[str, Any]) -> float | None:
    """The share of expected alternatives proposed for their line; None when none are expected."""
    expected: dict[str, str] = case.expected.get("alternatives") or {}
    if not expected:
        return None
    proposed = {line.get("sku"): line.get("alternative") for line in output.get("lines") or []}
    hits = sum(1 for sku, alt in expected.items() if proposed.get(sku) == alt)
    return hits / len(expected)
