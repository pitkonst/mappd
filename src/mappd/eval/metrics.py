"""Per-case scores and their aggregates; no process knowledge."""

import importlib
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from mappd.data.cases import Case
    from mappd.eval.config import Metric


def lookup(data: Mapping[str, Any], dotted: str) -> Any:
    """The value at a dotted path; None when any part is missing."""
    value: Any = data
    for part in dotted.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def in_scope(metric: Metric, case: Case) -> bool:
    """Whether the case's expected labels match the metric's `when`."""
    for key, allowed in metric.when.items():
        values = allowed if isinstance(allowed, list) else [allowed]
        if case.expected.get(key) not in values:
            return False
    return True


def _norm(value: object) -> str:
    return "" if value is None else str(value).strip().lower()


def _f1(expected: list[object], got: list[object]) -> float:
    want, have = {_norm(v) for v in expected}, {_norm(v) for v in got}
    if not want and not have:
        return 1.0
    hits = len(want & have)
    if hits == 0:
        return 0.0
    precision, recall = hits / len(have), hits / len(want)
    return 2 * precision * recall / (precision + recall)


def score_case(
    metric: Metric, case: Case, output: Mapping[str, Any] | None, judged: bool | None
) -> float | None:
    """The case's score in [0, 1]; None when out of scope, errored or not judged."""
    if output is None or not in_scope(metric, case):
        return None
    expected = lookup({"expected": case.expected}, metric.field) if metric.field else None
    got = lookup(output, metric.output_path()) if metric.output_path() else None
    match metric.kind:
        case "exact_match":
            return float(_norm(expected) == _norm(got))
        case "set_f1":
            return _f1(list(expected or []), list(got or []))
        case "rate":
            return float(_norm(got) == _norm(metric.equals))
        case "judge":
            return None if judged is None else float(judged)
        case "python":
            module_name, _, function = (metric.python or "").partition(":")
            fn = cast(
                "Callable[[Case, Mapping[str, Any]], float | None]",
                getattr(importlib.import_module(module_name), function),
            )
            return fn(case, output)


def aggregate(metric: Metric, scores: list[float | None]) -> float | None:
    """The mean of the scored cases; None when no case was in scope."""
    del metric  # every kind aggregates as a mean today
    scored = [s for s in scores if s is not None]
    return sum(scored) / len(scored) if scored else None


def passes(metric: Metric, value: float | None) -> bool | None:
    """Whether the value meets the threshold in the metric's direction; None without a value."""
    if value is None:
        return None
    return value <= metric.threshold if metric.lower_is_better else value >= metric.threshold
