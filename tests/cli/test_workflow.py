import json
import sys
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import stranded
from dbos import DBOS, SetWorkflowID
from typer.testing import CliRunner

from mappd import workflows
from mappd.cli import app
from mappd.cli import workflow as workflow_cli

if TYPE_CHECKING:
    from pathlib import Path

    from mappd.settings import Settings

runner = CliRunner()


@pytest.fixture
def env(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    monkeypatch.setenv("MAPPD_SYSTEM_DATABASE_URL", dbos_runtime.system_database_url)
    monkeypatch.setenv("MAPPD_APP_VERSION", "test")
    monkeypatch.setenv("MAPPD_APP_NAME", dbos_runtime.app_name)
    return dbos_runtime


def test_list_stranded_json(env: Settings) -> None:
    wid = stranded.strand(env, "old")
    try:
        r = runner.invoke(app, ["workflow", "list", "--stranded", "--json"])
        assert r.exit_code == 0, r.stderr
        rows = {w["workflow_id"]: w for w in json.loads(r.stdout)["workflows"]}
        assert rows[wid]["app_version"] == "old"
    finally:
        workflows.cancel(env, wid)


def test_recover_json_reports_moves(env: Settings) -> None:
    wid = stranded.strand(env, "old")
    r = runner.invoke(app, ["workflow", "recover", "--json"])
    assert r.exit_code == 0, r.stderr
    moved = {m["workflow_id"]: m for m in json.loads(r.stdout)["moved"]}
    assert moved[wid]["start_step"] == 2


def test_recover_refuses_to_target_dev_by_default(
    env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAPPD_APP_VERSION")
    r = runner.invoke(app, ["workflow", "recover"])
    assert r.exit_code == 2
    assert "MAPPD_APP_VERSION" in r.stderr


def test_cancel_unknown_exits_1(env: Settings) -> None:
    r = runner.invoke(app, ["workflow", "cancel", "no-such-workflow"])
    assert r.exit_code == 1
    assert "no-such-workflow" in r.stderr


def test_list_status_is_case_insensitive(env: Settings) -> None:
    wid = stranded.strand(env, "old")
    try:
        r = runner.invoke(app, ["workflow", "list", "--status", "pending", "--json"])
        assert r.exit_code == 0, r.stderr
        assert wid in {w["workflow_id"] for w in json.loads(r.stdout)["workflows"]}
    finally:
        workflows.cancel(env, wid)


def test_list_unknown_status_exits_2(env: Settings) -> None:
    assert runner.invoke(app, ["workflow", "list", "--status", "stuck"]).exit_code == 2


def test_recover_to_unknown_version_exits_2(env: Settings) -> None:
    r = runner.invoke(app, ["workflow", "recover", "--to-version", "typo"])
    assert r.exit_code == 2
    assert "typo" in r.stderr


def test_retry_json_names_the_fork(env: Settings) -> None:
    wid = stranded.fail()
    r = runner.invoke(app, ["workflow", "retry", wid, "--json"])
    assert r.exit_code == 0, r.stderr
    moved = json.loads(r.stdout)
    assert (moved["workflow_id"], moved["start_step"]) == (wid, 2)
    assert moved["new_workflow_id"] != wid


def test_retry_unknown_exits_1(env: Settings) -> None:
    r = runner.invoke(app, ["workflow", "retry", "no-such-workflow"])
    assert r.exit_code == 1
    assert "no-such-workflow" in r.stderr


def test_retry_not_in_error_exits_2(env: Settings) -> None:
    wid = stranded.strand(env, "old")
    try:
        r = runner.invoke(app, ["workflow", "retry", wid])
        assert r.exit_code == 2
        assert "PENDING" in r.stderr
    finally:
        workflows.cancel(env, wid)


def _done() -> str:
    wid = f"done-{uuid4()}"
    with SetWorkflowID(wid):
        DBOS.start_workflow(stranded.flaky_workflow).get_result()
    return wid


def test_show_prints_steps(env: Settings) -> None:
    wid = _done()
    r = runner.invoke(app, ["workflow", "show", wid])
    assert r.exit_code == 0, r.stderr
    assert "SUCCESS" in r.stdout
    assert "test.stranded.first" in r.stdout
    assert "test.flaky.step" in r.stdout


def test_show_json_has_steps_and_allowed(env: Settings) -> None:
    r = runner.invoke(app, ["workflow", "show", _done(), "--json"])
    assert r.exit_code == 0, r.stderr
    data = json.loads(r.stdout)
    assert [s["function_id"] for s in data["steps"]] == [1, 2]
    assert data["allowed"] == ["fork"]
    assert data["output"]["text"] == "ok"


def test_show_honours_payloads_off(env: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPPD_WORKFLOWS_PAYLOADS", "off")
    data = json.loads(runner.invoke(app, ["workflow", "show", _done(), "--json"]).stdout)
    assert data["input"] is None
    assert data["output"] is None


def test_show_unknown_exits_1(env: Settings) -> None:
    r = runner.invoke(app, ["workflow", "show", "no-such-workflow"])
    assert r.exit_code == 1
    assert "no-such-workflow" in r.stderr


def test_resume_cancelled_workflow(env: Settings) -> None:
    wid = f"parked-{uuid4()}"
    with workflows.client(env) as c:
        c.enqueue(
            {
                "workflow_name": "test.flaky",
                "queue_name": "test-unserved",
                "workflow_id": wid,
                "app_version": "test",
            }
        )
    workflows.cancel(env, wid)
    r = runner.invoke(app, ["workflow", "resume", wid, "--json"])
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout)["new_workflow_id"] == wid


def test_resume_running_exits_2(env: Settings) -> None:
    wid = stranded.strand(env, "old")
    try:
        r = runner.invoke(app, ["workflow", "resume", wid])
        assert r.exit_code == 2
        assert "PENDING" in r.stderr
    finally:
        workflows.cancel(env, wid)


def test_fork_from_step_prints_the_new_id(env: Settings) -> None:
    wid = _done()
    r = runner.invoke(app, ["workflow", "fork", wid, "--step", "2", "--json"])
    assert r.exit_code == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["workflow_id"] == wid
    assert data["start_step"] == 2
    assert data["new_workflow_id"] != wid


def test_fork_running_exits_2(env: Settings) -> None:
    wid = stranded.strand(env, "old")
    try:
        r = runner.invoke(app, ["workflow", "fork", wid, "--step", "1"])
        assert r.exit_code == 2
        assert "PENDING" in r.stderr
    finally:
        workflows.cancel(env, wid)


def test_fork_step_out_of_range_exits_2(env: Settings) -> None:
    r = runner.invoke(app, ["workflow", "fork", _done(), "--step", "9"])
    assert r.exit_code == 2
    assert "between 1 and 3" in r.stderr


def test_fork_unknown_exits_1(env: Settings) -> None:
    assert runner.invoke(app, ["workflow", "fork", "nope", "--step", "1"]).exit_code == 1


def test_cancel_finished_exits_2(env: Settings) -> None:
    r = runner.invoke(app, ["workflow", "cancel", _done()])
    assert r.exit_code == 2
    assert "finished" in r.stderr


@pytest.mark.parametrize("command", ["show", "resume", "fork"])
def test_help_has_examples(command: str) -> None:
    r = runner.invoke(app, ["workflow", command, "--help"])
    assert r.exit_code == 0
    assert "Example:" in r.stdout


def test_workflow_commands_can_decode_process_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("MAPPD_PROCESSES_DIR", str(tmp_path / "processes"))
    workflow_cli._processes_importable()
    assert sys.path[0] == str(tmp_path), "pickled inputs of processes.* decode"
