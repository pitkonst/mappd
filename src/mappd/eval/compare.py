"""Compare two eval runs; trivial baselines to read metrics against."""

from collections import Counter
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from mappd.eval.metrics import lookup

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mappd.data.cases import Case
    from mappd.eval.config import EvalConfig
    from mappd.eval.mlflow import Mlflow, RunRecord

BASELINE_TAG = "mappd.baseline"
SEARCH_DEPTH = 20
PAIR = 2


class CompareError(ValueError):
    """The runs to compare cannot be found or do not belong together."""


class Delta(BaseModel):
    """One metric before and after."""

    name: str
    before: float | None
    after: float | None
    change: float | None
    regression: bool


class Comparison(BaseModel):
    """What `mappd eval compare` prints."""

    before: str
    after: str
    set: str
    deltas: list[Delta]
    flipped: dict[str, list[str]]
    regressions: list[str]


def pick_runs(
    mlflow: Mlflow, experiment_id: str, run_ids: list[str], eval_set: str
) -> tuple[RunRecord, RunRecord]:
    """The two runs to compare, oldest first: the named ones, or the last two non-baseline runs."""
    if run_ids:
        if len(run_ids) != PAIR:
            raise CompareError("name two runs, or none for the last two runs of the set")
        before, after = (mlflow.get_run(r) for r in run_ids)
        if before.tags.get("mappd.set") != after.tags.get("mappd.set"):
            raise CompareError("the runs are from different sets")
        return before, after
    runs = [
        r
        for r in mlflow.latest_runs(experiment_id, eval_set, SEARCH_DEPTH)
        if BASELINE_TAG not in r.tags
    ][:PAIR]
    if len(runs) < PAIR:
        raise CompareError(f"compare needs two runs of the {eval_set} set; found {len(runs)}")
    return runs[1], runs[0]


def _flips(
    name: str, lower_is_better: bool, before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[str]:
    """Cases that went from good to bad: 1 → below 1, or for lower-is-better 0 → above 0."""
    good = 0.0 if lower_is_better else 1.0
    was = {row["id"]: (row.get("scores") or {}).get(name) for row in before}
    flipped = []
    for row in after:
        now = (row.get("scores") or {}).get(name)
        if was.get(row["id"]) == good and now is not None and now != good:
            flipped.append(row["id"])
    return sorted(flipped)


def compare(
    config: EvalConfig,
    before: RunRecord,
    after: RunRecord,
    before_cases: list[dict[str, Any]] | None,
    after_cases: list[dict[str, Any]] | None,
) -> Comparison:
    """Metric deltas, regressions beyond tolerance, and (dev) the cases that flipped."""
    deltas: list[Delta] = []
    flipped: dict[str, list[str]] = {}
    for metric in config.metrics:
        a, b = before.metrics.get(metric.name), after.metrics.get(metric.name)
        change = None if a is None or b is None else b - a
        worse = change is not None and (
            change > metric.tolerance if metric.lower_is_better else change < -metric.tolerance
        )
        deltas.append(Delta(name=metric.name, before=a, after=b, change=change, regression=worse))
        if before_cases is not None and after_cases is not None:
            ids = _flips(metric.name, metric.lower_is_better, before_cases, after_cases)
            if ids:
                flipped[metric.name] = ids
    return Comparison(
        before=before.run_id,
        after=after.run_id,
        set=after.tags.get("mappd.set", ""),
        deltas=deltas,
        flipped=flipped,
        regressions=[d.name for d in deltas if d.regression],
    )


def baseline_solver(
    fn: Callable[[dict[str, Any]], dict[str, Any]],
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """A sync baseline as the async solver the runner expects."""

    async def solve(input: dict[str, Any]) -> dict[str, Any]:  # noqa: A002 - the solver's API
        return fn(input)

    return solve


def majority_baseline(
    cases: list[Case], config: EvalConfig
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Predict, for every exact-match field, its most common expected value.

    Only top-level output paths are filled; a nested `output:` path is left out.
    """
    prediction: dict[str, Any] = {}
    for metric in config.metrics:
        if metric.kind != "exact_match" or metric.field is None or "." in metric.output_path():
            continue
        counts = Counter(lookup({"expected": c.expected}, metric.field) for c in cases)
        if counts:
            prediction[metric.output_path()] = counts.most_common(1)[0][0]
    return lambda _input: dict(prediction)
