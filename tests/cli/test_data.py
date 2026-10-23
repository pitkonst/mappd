import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mappd.cli import app
from mappd.data import synth

runner = CliRunner()
REPO = Path(__file__).parents[2]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPPD_PROCESSES_DIR", str(REPO / "processes"))
    monkeypatch.setenv("MAPPD_WORLD_PATH", str(REPO / "world.yaml"))


def test_synth_dry_run_json(env: None, tmp_path: Path) -> None:
    r = runner.invoke(
        app,
        [
            "data",
            "synth",
            "support",
            "--seed",
            "1",
            "--n",
            "20",
            "--dry-run",
            "--out",
            str(tmp_path),
            "--json",
        ],
    )
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout)["planned"] == {"reply": 10, "clarify": 4, "escalate": 6}


@pytest.mark.parametrize("mix", ["reply=.5", "reply=.5,shout=.5"])
def test_bad_mix_exits_2(env: None, mix: str, tmp_path: Path) -> None:
    r = runner.invoke(
        app,
        [
            "data",
            "synth",
            "support",
            "--seed",
            "1",
            "--n",
            "5",
            "--mix",
            mix,
            "--out",
            str(tmp_path),
            "--dry-run",
        ],
    )
    assert r.exit_code == 2


def test_unknown_process_exits_2(env: None) -> None:
    r = runner.invoke(app, ["data", "synth", "ghost", "--seed", "1", "--n", "5", "--dry-run"])
    assert r.exit_code == 2
    assert "no process 'ghost'" in r.stderr


def test_unrouted_models_exit_2(env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(synth, "list_models", lambda _s: ["fast"])
    r = runner.invoke(
        app, ["data", "synth", "support", "--seed", "1", "--n", "5", "--out", str(tmp_path)]
    )
    assert r.exit_code == 2
    assert "deploy/agentgateway" in r.stderr


def test_split_prints_counts_only(env: None, tmp_path: Path) -> None:
    lines = [
        json.dumps(
            {
                "id": f"s-{i}",
                "input": {"body": f"secret {i}"},
                "expected": {"outcome": "reply"},
                "meta": {"persona": f"p{i}"},
            }
        )
        for i in range(10)
    ]
    (tmp_path / "synth.jsonl").write_text("\n".join(lines) + "\n")
    r = runner.invoke(
        app,
        [
            "data",
            "split",
            "support",
            "--holdout",
            "0.2",
            "--seed",
            "1",
            "--out",
            str(tmp_path),
            "--json",
        ],
    )
    assert r.exit_code == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["dev"] + out["holdout"] == 10
    assert "secret" not in r.stdout
    assert (tmp_path / "holdout.jsonl").is_file()


def test_check_canary_passes_on_this_repository(env: None) -> None:
    r = runner.invoke(app, ["data", "check-canary", "--json"])
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout) == {"ok": True, "hits": []}


def test_check_canary_fails_with_a_planted_canary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607 - fixed git argv
    evals = tmp_path / "processes" / "demo" / "evals"
    evals.mkdir(parents=True)
    planted = "MAPPD-CANARY-planted"
    (evals / "canary.txt").write_text(planted + "\n")
    (tmp_path / "prompt.md").write_text(f"few-shot: {planted}\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)  # noqa: S603, S607 - fixed git argv
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAPPD_PROCESSES_DIR", str(tmp_path / "processes"))
    r = runner.invoke(app, ["data", "check-canary"])
    assert r.exit_code == 1
    assert "prompt.md:1" in r.stdout


def test_import_csv(env: None, tmp_path: Path) -> None:
    src = tmp_path / "h.csv"
    src.write_text("id,from,title,text\nT-1,a@x.example,Hi,Sensor offline\n")
    r = runner.invoke(
        app,
        [
            "data",
            "import",
            "support",
            str(src),
            "--map",
            "external_id=id,sender=from,subject=title,body=text",
            "--out",
            str(tmp_path),
            "--json",
        ],
    )
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout)["imported"] == 1


def test_split_reads_only_synth_or_imported(env: None, tmp_path: Path) -> None:
    r = runner.invoke(
        app,
        [
            "data",
            "split",
            "support",
            "--holdout",
            "0.2",
            "--seed",
            "1",
            "--from",
            "holdout",
            "--out",
            str(tmp_path),
        ],
    )
    assert r.exit_code == 2
    assert not (tmp_path / "dev.jsonl").exists()


def test_split_of_a_malformed_file_exits_2_quietly(env: None, tmp_path: Path) -> None:
    (tmp_path / "synth.jsonl").write_text('{"id": "b", "input": {"body": "SECRET-TEXT"}}\n')
    r = runner.invoke(
        app, ["data", "split", "support", "--holdout", "0.2", "--seed", "1", "--out", str(tmp_path)]
    )
    assert r.exit_code == 2
    assert "line 1" in r.stderr
    assert "SECRET-TEXT" not in r.stdout + r.stderr


def test_synth_passes_concurrency(
    env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run(*_args: object, **kwargs: object) -> synth.Report:
        seen.update(kwargs)
        return synth.Report(planned={"reply": 1})

    monkeypatch.setattr(synth, "run", fake_run)
    args = ["data", "synth", "support", "--seed", "1", "--n", "1", "--concurrency", "3", "--json"]
    r = runner.invoke(app, args)
    assert r.exit_code == 0, r.stderr
    assert seen["concurrency"] == 3
    assert runner.invoke(app, [*args[:-3], "--concurrency", "0"]).exit_code == 2
