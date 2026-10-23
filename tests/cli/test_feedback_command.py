"""mappd feedback sync: merge a process's decided cases into evals/feedback.jsonl."""

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from mappd.cli import app
from mappd.data.cases import read_cases

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

DEMO_INIT = """
from datetime import datetime

from mappd.data.cases import Case
from mappd.feedback import FeedbackSpec
from mappd.process import Process


async def collect(since: datetime) -> list[str]:
    return ["1", "2"]


def to_case(record: str) -> Case:
    return Case(id=f"fb-{record}", input={"id": record}, expected={"outcome": "reply"},
                meta={"source": "feedback", "edited": record == "2"})


process = Process(name="demo", feedback=FeedbackSpec(collect=collect, to_case=to_case))
"""


@pytest.fixture
def demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # a fresh package name: the registry caches packages by name across tests
    root = tmp_path / f"procs_{uuid4().hex[:8]}"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "__init__.py").write_text(DEMO_INIT)
    (root / "plain").mkdir()
    (root / "plain" / "__init__.py").write_text(
        "from mappd.process import Process\n\nprocess = Process(name='plain')\n"
    )
    (root / "__init__.py").write_text(
        "from .demo import process as demo\nfrom .plain import process as plain\n\n"
        "PROCESSES = (demo, plain)\n"
    )
    monkeypatch.setenv("MAPPD_PROCESSES_DIR", str(root))
    return root / "demo" / "evals"


def test_sync_writes_cases_with_the_canary(demo: Path) -> None:
    r = runner.invoke(app, ["feedback", "sync", "demo", "--json"])
    assert r.exit_code == 0, r.stderr
    out = json.loads(r.stdout)
    assert (out["new"], out["edited"]) == (2, 1)
    canary = (demo / "canary.txt").read_text().strip()
    cases = read_cases(demo / "feedback.jsonl")
    assert {c.meta["canary"] for c in cases} == {canary}
    again = json.loads(runner.invoke(app, ["feedback", "sync", "demo", "--json"]).stdout)
    assert (again["new"], again["unchanged"]) == (0, 2)


@pytest.mark.parametrize(
    "args",
    [
        ["feedback", "sync", "nope"],
        ["feedback", "sync", "plain"],
        ["feedback", "sync", "demo", "--since", "7x"],
    ],
)
def test_bad_input_exits_2(demo: Path, args: list[str]) -> None:
    assert runner.invoke(app, args).exit_code == 2


def test_out_writes_elsewhere(demo: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "fb.jsonl"
    r = runner.invoke(app, ["feedback", "sync", "demo", "--out", str(target), "--json"])
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout)["path"] == str(target)
    assert len(read_cases(target)) == 2
    assert not (demo / "feedback.jsonl").exists()
