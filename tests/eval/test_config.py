import re
from pathlib import Path

import pytest

from mappd.eval.config import EvalConfigError, load_config


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def one(line: str) -> str:
    return f"metrics:\n  - {{{line}}}\n"


def test_defaults_and_output_path(tmp_path: Path) -> None:
    config = load_config(
        write(tmp_path, one("name: r, kind: exact_match, field: expected.route, threshold: 0.8"))
    )
    metric = config.metrics[0]
    assert (metric.tolerance, metric.lower_is_better, metric.when) == (0.03, False, {})
    assert metric.output_path() == "route"


def test_explicit_output_path(tmp_path: Path) -> None:
    line = "name: c, kind: set_f1, field: expected.citations, output: draft.citations, threshold: 1"
    config = load_config(write(tmp_path, one(line)))
    assert config.metrics[0].output_path() == "draft.citations"


def test_judge_rate_and_python_metrics_do_not_require_expected_field(tmp_path: Path) -> None:
    text = (
        "metrics:\n"
        "  - {name: j, kind: judge, threshold: 0.7}\n"
        "  - {name: r, kind: rate, output: outcome, equals: escalate, threshold: 0.1}\n"
        "  - {name: p, kind: python, python: 'mod:fn', threshold: 0.5}\n"
    )
    config = load_config(write(tmp_path, text))
    assert [m.name for m in config.metrics] == ["j", "r", "p"]
    assert config.metrics[1].output_path() == "outcome"


@pytest.mark.parametrize(
    ("text", "where"),
    [
        (one("name: r, kind: nope, field: expected.route, threshold: 1"), "metrics[0].kind"),
        (one("name: r, kind: exact_match, field: route, threshold: 1"), "metrics[0].field"),
        (one("name: r, kind: rate, field: expected.outcome, threshold: 1"), "metrics[0].equals"),
        (one("name: r, kind: python, field: expected.x, threshold: 1"), "metrics[0].python"),
        ("metrics: [\n", "config.yaml"),
    ],
)
def test_errors_name_the_field(tmp_path: Path, text: str, where: str) -> None:
    with pytest.raises(EvalConfigError, match=re.escape(where)):
        load_config(write(tmp_path, text))


def test_duplicate_names_are_rejected(tmp_path: Path) -> None:
    line = "  - {name: r, kind: exact_match, field: expected.route, threshold: 1}\n"
    with pytest.raises(EvalConfigError, match="duplicate metric r"):
        load_config(write(tmp_path, "metrics:\n" + line + line))


def test_the_support_config_loads() -> None:
    names = {m.name for m in load_config(Path("processes/support/evals/config.yaml")).metrics}
    assert names == {
        "routing_accuracy",
        "outcome_accuracy",
        "escalation_role_accuracy",
        "citations_f1",
        "false_escalation_rate",
        "judge_pass_rate",
    }
