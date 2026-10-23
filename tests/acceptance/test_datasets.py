import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import stack

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(900)]
REPO = Path(__file__).parents[2]


def mappd(*args: str, cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "MAPPD_GATEWAY_URL": "http://127.0.0.1:14000/v1"}
    return subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "mappd", *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_synth_split_and_canary_through_the_gateway(tmp_path: Path) -> None:
    r = mappd(
        "data",
        "synth",
        "support",
        "--seed",
        "42",
        "--n",
        "60",
        "--out",
        str(tmp_path),
        "--cache-dir",
        str(tmp_path / "cache"),
        "--json",
    )
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    assert sum(report["kept"].values()) >= 40
    r = mappd(
        "data",
        "split",
        "support",
        "--holdout",
        "0.2",
        "--seed",
        "7",
        "--out",
        str(tmp_path),
        "--json",
    )
    assert r.returncode == 0, r.stderr
    assert mappd("data", "check-canary").returncode == 0


def test_the_knowledge_base_comes_from_the_world() -> None:
    out = stack.compose("exec", "-T", "app", "python", "-m", "processes.support.data.seed")
    assert "knowledge base: 20 articles" in out.stdout
