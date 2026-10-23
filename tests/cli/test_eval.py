import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from mappd import telemetry
from mappd.cli import app
from mappd.cli import evals as eval_cli
from mappd.eval.compare import CompareError, Comparison, Delta
from mappd.eval.runner import EvalReport, MetricResult
from mappd.settings import Settings

runner = CliRunner()
REPO = Path(__file__).parents[2]


def report(eval_set: str = "dev", passed: bool = True) -> EvalReport:
    metric = MetricResult(
        name="routing_accuracy",
        value=0.9 if passed else 0.5,
        threshold=0.8,
        lower_is_better=False,
        passed=passed,
        cases=3,
    )
    return EvalReport(
        run_id="r1",
        set=eval_set,  # type: ignore[arg-type]
        cases=3,
        errored=0,
        judge_errored=0,
        metrics=[metric],
        failing=[] if eval_set == "holdout" else ["c2"],
        logged=True,
        failed=not passed,
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("MAPPD_PROCESSES_DIR", str(REPO / "processes"))
    seen: dict[str, Any] = {"setup": 0, "pass": True}

    async def run_eval(*_a: Any, **kw: Any) -> EvalReport:
        seen["kw"] = kw
        return report(kw["eval_set"], seen["pass"])

    def start_tracing(*_a: Any) -> None:
        seen["setup"] += 1

    monkeypatch.setattr(eval_cli, "run_eval", run_eval)
    monkeypatch.setattr(eval_cli, "routed_models", lambda _s: ["fast", "smart", "embed", "judge"])
    monkeypatch.setattr(eval_cli, "start_tracing", start_tracing)
    monkeypatch.setattr(eval_cli, "experiment_id", lambda *_a: "7")
    return seen


def test_start_tracing_targets_the_eval_experiment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eval traces are exported to the process's eval experiment."""
    seen: list[Settings] = []
    monkeypatch.setattr(telemetry, "setup", seen.append)
    eval_cli.start_tracing(Settings(mlflow_experiment_id="0"), "7")
    assert [s.mlflow_experiment_id for s in seen] == ["7"]


def test_eval_run_dev_prints_metrics_and_failing_ids(fake: dict[str, Any]) -> None:
    r = runner.invoke(app, ["eval", "run", "support", "--json"])
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout)["failing"] == ["c2"]
    assert fake["setup"] == 1
    assert fake["kw"]["eval_set"] == "dev"


def test_eval_run_below_threshold_exits_1(fake: dict[str, Any]) -> None:
    fake["pass"] = False
    r = runner.invoke(app, ["eval", "run", "support"])
    assert r.exit_code == 1
    assert "FAIL" in r.stdout


def test_eval_run_holdout_refuses_limit_and_prints_no_ids(fake: dict[str, Any]) -> None:
    r = runner.invoke(app, ["eval", "run", "support", "--set", "holdout", "--limit", "5"])
    assert r.exit_code == 2
    r = runner.invoke(app, ["eval", "run", "support", "--set", "holdout"])
    assert r.exit_code == 0, r.stderr
    assert "c2" not in r.stdout
    assert fake["setup"] == 0, "holdout runs never install tracing"


def test_eval_run_needs_the_judge_routed(
    fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake
    monkeypatch.setattr(eval_cli, "routed_models", lambda _s: ["fast"])
    r = runner.invoke(app, ["eval", "run", "support"])
    assert r.exit_code == 2
    assert "judge" in r.stderr


def test_eval_run_unknown_process_exits_2(fake: dict[str, Any]) -> None:
    del fake
    r = runner.invoke(app, ["eval", "run", "nope"])
    assert r.exit_code == 2
    assert "no process 'nope'" in r.stderr


def test_eval_compare_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    def regressed(*_a: Any, **_k: Any) -> Comparison:
        delta = Delta(name="routing_accuracy", before=0.9, after=0.8, change=-0.1, regression=True)
        return Comparison(
            before="a",
            after="b",
            set="dev",
            deltas=[delta],
            flipped={"routing_accuracy": ["c1"]},
            regressions=["routing_accuracy"],
        )

    monkeypatch.setattr(eval_cli, "compare_runs", regressed)
    r = runner.invoke(app, ["eval", "compare", "support"])
    assert r.exit_code == 1
    assert "REGRESSION" in r.stdout
    assert "c1" in r.stdout

    def missing(*_a: Any, **_k: Any) -> Comparison:
        raise CompareError("compare needs two runs of the dev set; found 1")

    monkeypatch.setattr(eval_cli, "compare_runs", missing)
    r = runner.invoke(app, ["eval", "compare", "support"])
    assert r.exit_code == 2
    assert "two runs" in r.stderr


def test_eval_help_has_examples() -> None:
    for command in ("run", "baseline", "compare"):
        out = runner.invoke(app, ["eval", command, "--help"]).stdout
        assert "Example" in out


def test_eval_run_without_the_set_file_exits_2(
    fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake

    async def missing(*_a: Any, **_k: Any) -> EvalReport:
        raise FileNotFoundError("processes/support/evals/holdout.jsonl")

    monkeypatch.setattr(eval_cli, "run_eval", missing)
    r = runner.invoke(app, ["eval", "run", "support", "--set", "holdout"])
    assert r.exit_code == 2
    assert "holdout.jsonl" in r.stderr
