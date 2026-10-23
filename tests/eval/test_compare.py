from typing import Any

import pytest

from mappd.data.cases import Case
from mappd.eval.compare import CompareError, compare, majority_baseline, pick_runs
from mappd.eval.config import EvalConfig
from mappd.eval.mlflow import RunRecord

CONFIG = EvalConfig.model_validate(
    {
        "metrics": [
            {
                "name": "routing_accuracy",
                "kind": "exact_match",
                "field": "expected.route",
                "threshold": 0.8,
            },
            {
                "name": "false_escalation_rate",
                "kind": "rate",
                "field": "expected.outcome",
                "equals": "escalate",
                "threshold": 0.15,
                "lower_is_better": True,
            },
        ]
    }
)


def record(run_id: str, metrics: dict[str, float], eval_set: str = "dev", **tags: str) -> RunRecord:
    return RunRecord(run_id, 0, {}, {"mappd.set": eval_set, **tags}, metrics, "")


def test_drops_beyond_tolerance_are_regressions_in_either_direction() -> None:
    before = record("a", {"routing_accuracy": 0.90, "false_escalation_rate": 0.10})
    after = record("b", {"routing_accuracy": 0.86, "false_escalation_rate": 0.14})
    result = compare(CONFIG, before, after, None, None)
    assert result.regressions == ["routing_accuracy", "false_escalation_rate"]
    within = record("c", {"routing_accuracy": 0.88, "false_escalation_rate": 0.12})
    assert compare(CONFIG, before, within, None, None).regressions == []


def test_a_metric_missing_on_one_side_is_not_a_regression() -> None:
    result = compare(CONFIG, record("a", {}), record("b", {"routing_accuracy": 0.1}), None, None)
    assert result.regressions == []


def test_cases_that_flip_are_listed() -> None:
    def rows(scores: dict[str, float]) -> list[dict[str, Any]]:
        return [{"id": cid, "scores": {"routing_accuracy": s}} for cid, s in scores.items()]

    result = compare(
        CONFIG,
        record("a", {"routing_accuracy": 1.0}),
        record("b", {"routing_accuracy": 0.5}),
        rows({"c1": 1.0, "c2": 1.0}),
        rows({"c1": 1.0, "c2": 0.0}),
    )
    assert result.flipped == {"routing_accuracy": ["c2"]}


class Runs:
    def __init__(self, runs: list[RunRecord]) -> None:
        self.runs = runs

    def latest_runs(self, _e: str, eval_set: str, n: int) -> list[RunRecord]:
        return [r for r in self.runs if r.tags["mappd.set"] == eval_set][:n]

    def get_run(self, run_id: str) -> RunRecord:
        return next(r for r in self.runs if r.run_id == run_id)


def test_default_is_the_last_two_non_baseline_runs_oldest_first() -> None:
    base = record("base", {}, **{"mappd.baseline": "keywords"})
    runs = Runs([record("new", {}), base, record("old", {})])
    before, after = pick_runs(runs, "7", [], "dev")  # type: ignore[arg-type]
    assert (before.run_id, after.run_id) == ("old", "new")


def test_compare_needs_two_runs_of_one_set() -> None:
    with pytest.raises(CompareError, match="two runs"):
        pick_runs(Runs([record("only", {})]), "7", [], "dev")  # type: ignore[arg-type]
    mixed = Runs([record("a", {}), record("b", {}, "holdout")])
    with pytest.raises(CompareError, match="different sets"):
        pick_runs(mixed, "7", ["a", "b"], "dev")  # type: ignore[arg-type]
    with pytest.raises(CompareError, match="two runs"):
        pick_runs(mixed, "7", ["a"], "dev")  # type: ignore[arg-type]


def test_majority_baseline_predicts_the_most_common_label() -> None:
    cases = [
        Case(id=str(i), input={}, expected={"route": r, "outcome": "reply"}, meta={})
        for i, r in enumerate(["technical", "technical", "commercial"])
    ]
    predict = majority_baseline(cases, CONFIG)
    assert predict({})["route"] == "technical"


def test_flips_follow_the_metric_direction() -> None:
    def rows(scores: dict[str, float]) -> list[dict[str, Any]]:
        return [{"id": cid, "scores": {"false_escalation_rate": s}} for cid, s in scores.items()]

    result = compare(
        CONFIG,
        record("a", {"false_escalation_rate": 0.5}),
        record("b", {"false_escalation_rate": 0.5}),
        rows({"fixed": 1.0, "broke": 0.0}),
        rows({"fixed": 0.0, "broke": 1.0}),
    )
    assert result.flipped == {"false_escalation_rate": ["broke"]}
