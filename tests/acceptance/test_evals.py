import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import stack

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(900)]
REPO = Path(__file__).parents[2]
ENV = {
    **os.environ,
    "MAPPD_GATEWAY_URL": "http://127.0.0.1:14000/v1",
    "MAPPD_DATABASE_URL": f"{stack.PG}/mappd",
    "MAPPD_MLFLOW_URL": "http://127.0.0.1:15001",
    "MAPPD_GATEWAY_CONFIG": "deploy/agentgateway/gateway.mocks.yaml",
}


def mappd(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "mappd", *args],
        env=ENV,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def test_run_baseline_and_compare_through_the_stack(tmp_path: Path) -> None:
    stack.compose("exec", "-T", "app", "python", "-m", "processes.support.data.seed")
    cache = ["--cache-dir", str(tmp_path / "cache")]
    first = mappd("eval", "run", "support", "--limit", "20", "--json", *cache)
    assert first.returncode in {0, 1}, first.stderr
    report = json.loads(first.stdout)
    assert report["logged"], first.stderr
    assert (report["cases"], report["errored"]) == (20, 0)
    assert {m["name"] for m in report["metrics"]} >= {"routing_accuracy", "judge_pass_rate"}
    base = mappd("eval", "baseline", "support", "--json", *cache)
    assert base.returncode in {0, 1}, base.stderr
    labels = {b["label"] for b in json.loads(base.stdout)["baselines"]}
    assert {"majority", "keywords"} <= labels
    second = mappd("eval", "run", "support", "--limit", "20", "--json", *cache)
    assert second.returncode in {0, 1}, second.stderr
    cmp = mappd("eval", "compare", "support", "--json")
    assert cmp.returncode == 0, cmp.stdout + cmp.stderr  # same mock answers: no regression
    result = json.loads(cmp.stdout)
    assert (result["before"], result["after"]) == (
        report["run_id"],
        json.loads(second.stdout)["run_id"],
    )
