import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from demo_process import make_process
from typer.testing import CliRunner

from mappd.cli import app
from mappd.process import register_process

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mappd.settings import Settings

runner = CliRunner()


@pytest.fixture
def processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Not "processes": that name is the repository's own package once anything imports it.
    root = tmp_path / f"procs_{uuid4().hex}"
    monkeypatch.setenv("MAPPD_PROCESSES_DIR", str(root))
    monkeypatch.setattr(sys, "path", list(sys.path))
    return root


def test_list(processes: Path) -> None:
    make_process(processes, "demo")
    r = runner.invoke(app, ["process", "list", "--json"])
    assert json.loads(r.stdout) == {
        "processes": [{"name": "demo", "api": True, "migrations": 1, "shadow": None, "auto": []}]
    }


def test_new_is_gone(processes: Path) -> None:
    # Processes start from a demo (support, rfq, installation), not from a scaffold.
    r = runner.invoke(app, ["process", "new", "demo"])
    assert r.exit_code == 2


def test_list_without_processes(processes: Path) -> None:
    r = runner.invoke(app, ["process", "list"])
    assert r.exit_code == 0
    assert r.stdout.strip() == "no processes yet; copy a demo from the repository's processes/"


def test_run_with_unreadable_input_exits_2(processes: Path, tmp_path: Path) -> None:
    make_process(processes, "demo")
    r = runner.invoke(app, ["process", "run", "demo", "--input", str(tmp_path / "missing.json")])
    assert r.exit_code == 2


def test_run_unknown_process_exits_2(processes: Path, tmp_path: Path) -> None:
    case = tmp_path / "case.json"
    case.write_text("{}")
    r = runner.invoke(app, ["process", "run", "ghost", "--input", str(case)])
    assert r.exit_code == 2
    assert "ghost" in r.stderr


@pytest.mark.timeout(120)
def test_run_end_to_end(recreate_database: Callable[[str], str], tmp_path: Path) -> None:
    app_db = recreate_database("mappd_test_run")
    env = {
        **os.environ,
        "MAPPD_DATABASE_URL": app_db,
        "MAPPD_SYSTEM_DATABASE_URL": recreate_database("mappd_test_run_dbos"),
        "MAPPD_PROCESSES_DIR": str(tmp_path / "processes"),
        "MAPPD_APP_VERSION": "cli-test",
        "MAPPD_MLFLOW_URL": "http://127.0.0.1:9",  # nothing listens: exports fail fast
        "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT": "1",
    }

    def mappd(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv
            [sys.executable, "-m", "mappd", *args],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    make_process(tmp_path / "processes", "demo")
    assert mappd("db", "migrate").returncode == 0
    case = tmp_path / "case.json"
    case.write_text('{"external_id": "e2e-1", "text": "I want a refund"}')
    r = mappd("process", "run", "demo", "--input", str(case), "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["output"] == {"outcome": "escalated", "reason": "hard rule: refund"}
    assert out["workflow_id"].startswith("demo.run:")
    assert len(out["trace_id"]) == 32
    assert out["trace_id"] != "0" * 32
    with psycopg.connect(app_db) as conn:
        status = conn.execute("SELECT status FROM demo_cases WHERE external_id='e2e-1'").fetchone()
    assert status == ("escalated",)


def test_seed_unknown_process_exits_2(processes: Path) -> None:
    r = runner.invoke(app, ["process", "seed", "ghost"])
    assert r.exit_code == 2
    assert "ghost" in r.stderr


def test_seed_without_seed_module_exits_2(processes: Path) -> None:
    make_process(processes, "unseeded")
    r = runner.invoke(app, ["process", "seed", "unseeded"])
    assert r.exit_code == 2
    assert "no seed function" in r.stderr


def test_seed_runs_the_seed_function(processes: Path) -> None:
    demo = processes / "demo"
    demo.mkdir(parents=True)
    (demo / "__init__.py").write_text(
        "from mappd.process import Process\n\n"
        "def seed() -> int:\n"
        "    return 3\n\n"
        "process = Process(name='demo', seed=seed)\n"
    )
    register_process(processes, "demo")
    r = runner.invoke(app, ["process", "seed", "demo", "--json"])
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout) == {"process": "demo", "seeded": 3}
    plain = runner.invoke(app, ["process", "seed", "demo"])
    assert plain.stdout.strip() == "seeded demo: 3 rows"


def test_seed_says_what_it_counted(processes: Path) -> None:
    demo = processes / "demo"
    demo.mkdir(parents=True)
    (demo / "__init__.py").write_text(
        "from mappd.process import Process\n\n"
        "def seed() -> int:\n"
        "    return 20\n\n"
        "process = Process(name='demo', seed=seed, seeds='knowledge-base articles')\n"
    )
    register_process(processes, "demo")
    r = runner.invoke(app, ["process", "seed", "demo"])
    assert r.stdout.strip() == "seeded demo: 20 knowledge-base articles"


def test_seed_runs_an_async_seed(processes: Path) -> None:
    demo = processes / "demo"
    demo.mkdir(parents=True)
    (demo / "__init__.py").write_text(
        "import asyncio\n"
        "from mappd.process import Process\n\n"
        "async def seed() -> int:\n"
        "    await asyncio.sleep(0)\n"
        "    return 7\n\n"
        "process = Process(name='demo', seed=seed)\n"
    )
    register_process(processes, "demo")
    r = runner.invoke(app, ["process", "seed", "demo"])
    assert r.exit_code == 0, r.stderr
    assert "7" in r.stdout


def test_run_as_dev_against_a_versioned_database_exits_2(
    processes: Path, dbos_runtime: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAPPD_SYSTEM_DATABASE_URL", dbos_runtime.system_database_url)
    monkeypatch.setenv("MAPPD_APP_NAME", dbos_runtime.app_name)
    monkeypatch.delenv("MAPPD_APP_VERSION", raising=False)
    make_process(processes, "versioned_demo")
    case = tmp_path / "case.json"
    case.write_text('{"external_id": "x", "text": "hi"}')
    r = runner.invoke(app, ["process", "run", "versioned_demo", "--input", str(case)])
    assert r.exit_code == 2
    assert "MAPPD_APP_VERSION" in r.stderr


DEMO_INIT = """
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from mappd.autonomy import categories
from mappd.process import Process


class Out(BaseModel):
    topic: Literal["how_to", "billing"]


process = Process(
    name="demo",
    autonomy=Path(__file__).parent / "autonomy.yaml",
    categories=categories(Out, "topic"),
)
"""


@pytest.fixture
def demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # a fresh package name: the registry caches packages by name across tests
    root = tmp_path / f"procs_{uuid4().hex[:8]}"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "__init__.py").write_text(DEMO_INIT)
    (root / "__init__.py").write_text("from .demo import process as demo\n\nPROCESSES = (demo,)\n")
    monkeypatch.setenv("MAPPD_PROCESSES_DIR", str(root))
    return root / "demo" / "autonomy.yaml"


def test_shadow_on_writes_the_file_and_prints_the_diff(demo: Path) -> None:
    r = runner.invoke(app, ["process", "shadow", "demo", "on", "--json"])
    assert r.exit_code == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["changed"] is True
    assert "+shadow: true" in out["diff"]
    assert "shadow: true" in demo.read_text()


def test_autonomy_sets_a_category(demo: Path) -> None:
    r = runner.invoke(app, ["process", "autonomy", "demo", "how_to", "auto"])
    assert r.exit_code == 0, r.stderr
    assert "commit" in r.stdout
    again = runner.invoke(app, ["process", "autonomy", "demo", "how_to", "auto", "--json"])
    assert json.loads(again.stdout)["changed"] is False


@pytest.mark.parametrize(
    "args",
    [
        ["process", "autonomy", "demo", "refund", "auto"],
        ["process", "autonomy", "demo", "how_to", "sometimes"],
        ["process", "shadow", "nope", "on"],
        ["process", "shadow", "demo", "maybe"],
    ],
)
def test_bad_input_exits_2(demo: Path, args: list[str]) -> None:
    assert runner.invoke(app, args).exit_code == 2


def test_list_shows_shadow_and_auto_categories(demo: Path) -> None:
    runner.invoke(app, ["process", "autonomy", "demo", "billing", "auto"])
    r = runner.invoke(app, ["process", "list", "--json"])
    [row] = json.loads(r.stdout)["processes"]
    assert (row["shadow"], row["auto"]) == (False, ["billing"])


def test_shadow_on_a_broken_policy_exits_2_and_leaves_it(demo: Path) -> None:
    demo.write_text("shadwo: true\n")
    r = runner.invoke(app, ["process", "shadow", "demo", "on"])
    assert r.exit_code == 2
    assert "shadwo" in r.stderr
    assert demo.read_text() == "shadwo: true\n"
