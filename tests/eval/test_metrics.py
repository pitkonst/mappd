import sys
import types
from typing import Any

import pytest

from mappd.data.cases import Case
from mappd.eval.config import Metric
from mappd.eval.metrics import aggregate, in_scope, lookup, passes, score_case


def case(**expected: Any) -> Case:
    return Case(id="c", input={}, expected=expected, meta={})


def metric(**fields: Any) -> Metric:
    return Metric.model_validate({"name": "m", "threshold": 0.5, **fields})


def test_lookup_follows_dots_and_tolerates_gaps() -> None:
    data = {"draft": {"citations": ["kb-1"]}, "x": None}
    assert lookup(data, "draft.citations") == ["kb-1"]
    assert lookup(data, "x.y") is None
    assert lookup(data, "missing") is None


def test_exact_match_is_case_insensitive_and_none_aware() -> None:
    m = metric(kind="exact_match", field="expected.route")
    assert score_case(m, case(route="Commercial"), {"route": "commercial"}, None) == 1.0
    assert score_case(m, case(route="technical"), {"route": "commercial"}, None) == 0.0
    role = metric(kind="exact_match", field="expected.escalate_to")
    assert score_case(role, case(escalate_to=None), {"escalate_to": None}, None) == 1.0


def test_when_selects_by_expected_labels() -> None:
    m = metric(kind="exact_match", field="expected.escalate_to", when={"outcome": "escalate"})
    assert in_scope(m, case(outcome="escalate"))
    assert not in_scope(m, case(outcome="reply"))
    many = metric(
        kind="exact_match", field="expected.route", when={"outcome": ["reply", "clarify"]}
    )
    assert in_scope(many, case(outcome="clarify"))
    assert (
        score_case(m, case(outcome="reply", escalate_to=None), {"escalate_to": "x"}, None) is None
    )


@pytest.mark.parametrize(
    ("expected", "got", "f1"),
    [
        (["a"], ["a"], 1.0),
        (["a"], [], 0.0),
        ([], [], 1.0),
        (["a", "b"], ["a"], 2 / 3),
        (["a"], None, 0.0),
    ],
)
def test_set_f1(expected: list[str], got: list[str] | None, f1: float) -> None:
    m = metric(kind="set_f1", field="expected.citations", output="draft.citations")
    output = {"draft": None if got is None else {"citations": got}}
    assert score_case(m, case(citations=expected), output, None) == pytest.approx(f1)


def test_rate_counts_the_output_value() -> None:
    m = metric(
        kind="rate",
        field="expected.outcome",
        output="outcome",
        equals="escalate",
        lower_is_better=True,
    )
    assert score_case(m, case(outcome="reply"), {"outcome": "escalate"}, None) == 1.0
    assert score_case(m, case(outcome="reply"), {"outcome": "reply"}, None) == 0.0


def test_judge_uses_the_verdict() -> None:
    m = metric(kind="judge", field="expected.outcome")
    assert score_case(m, case(outcome="reply"), {}, True) == 1.0
    assert score_case(m, case(outcome="reply"), {}, False) == 0.0
    assert score_case(m, case(outcome="reply"), {}, None) is None


def test_python_metric_calls_the_function(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("tests_eval_custom")
    module.always_half = lambda _case, _output: 0.5  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tests_eval_custom", module)
    m = metric(kind="python", field="expected.outcome", python="tests_eval_custom:always_half")
    assert score_case(m, case(outcome="reply"), {}, None) == 0.5


def test_errored_case_scores_none() -> None:
    m = metric(kind="exact_match", field="expected.route")
    assert score_case(m, case(route="technical"), None, None) is None


def test_metric_with_no_cases_in_scope_is_none() -> None:
    m = metric(kind="exact_match", field="expected.route")
    assert aggregate(m, [None, None]) is None
    assert passes(m, None) is None


def test_aggregate_and_passes_respect_direction() -> None:
    up = metric(kind="exact_match", field="expected.route", threshold=0.8)
    assert aggregate(up, [1.0, 0.0, None, 1.0]) == pytest.approx(2 / 3)
    assert passes(up, 0.8) is True
    assert passes(up, 0.79) is False
    down = metric(
        kind="rate",
        field="expected.outcome",
        equals="escalate",
        threshold=0.15,
        lower_is_better=True,
    )
    assert passes(down, 0.15) is True
    assert passes(down, 0.2) is False
