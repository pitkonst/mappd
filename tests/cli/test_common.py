import json
import logging
from typing import TYPE_CHECKING

import pytest
import typer
from typer.testing import CliRunner

from mappd.cli import app
from mappd.cli._common import (
    EXIT_USAGE,
    JsonFlag,
    configure_logging,
    emit,
    fail,
    load_settings,
    progress,
    status,
    table,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

runner = CliRunner()


@pytest.fixture
def bare_root_logger() -> Iterator[logging.Logger]:
    """Root logger restored afterwards; pytest's handlers cleared per test."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        yield root
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def test_configure_logging_wins_over_a_later_basicconfig(
    bare_root_logger: logging.Logger,
) -> None:
    bare_root_logger.handlers.clear()
    configure_logging()
    configured = bare_root_logger.handlers[:]
    assert bare_root_logger.level == logging.WARNING
    logging.basicConfig(  # what mcp's MCPServer does at import time
        level=logging.INFO, handlers=[logging.StreamHandler()]
    )
    assert bare_root_logger.level == logging.WARNING
    assert bare_root_logger.handlers == configured


def test_configure_logging_reads_mappd_log_level(
    bare_root_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAPPD_LOG_LEVEL", "info")
    bare_root_logger.handlers.clear()
    configure_logging()
    assert bare_root_logger.level == logging.INFO


def test_configure_logging_rejects_a_bad_level(
    bare_root_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    del bare_root_logger
    monkeypatch.setenv("MAPPD_LOG_LEVEL", "loud")
    with pytest.raises(typer.Exit) as exc:
        configure_logging()
    assert exc.value.exit_code == 2


def test_emit_prints_json_or_lines(capsys: pytest.CaptureFixture[str]) -> None:
    emit({"a": 1}, True, ["ignored"])
    emit({"a": 1}, False, ["first", "second"])
    out = capsys.readouterr().out.splitlines()
    assert json.loads(out[0]) == {"a": 1}
    assert out[1:] == ["first", "second"]


def test_fail_prints_to_stderr_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as exc:
        fail("boom", 3)
    assert exc.value.exit_code == 3
    assert capsys.readouterr().err == "error: boom\n"


def test_fail_prints_json_when_the_command_passed_json() -> None:
    probe = typer.Typer()

    @probe.command()
    def cmd(as_json: JsonFlag = False) -> None:
        fail("boom", 3)

    r = runner.invoke(probe, ["--json"])
    assert r.exit_code == 3
    assert json.loads(r.stdout) == {"ok": False, "error": "boom", "exit_code": 3}
    assert r.stderr == ""

    r = runner.invoke(probe, [])
    assert r.exit_code == 3
    assert r.stderr == "error: boom\n"
    assert r.stdout == ""


def test_a_real_command_reports_errors_as_json() -> None:
    r = runner.invoke(app, ["action", "resolve", "k", "--took-effect", "--not-sent", "--json"])
    assert r.exit_code == 2
    assert json.loads(r.stdout) == {
        "ok": False,
        "error": "pass exactly one of --took-effect and --not-sent",
        "exit_code": 2,
    }
    assert r.stderr == ""


def test_json_mode_does_not_leak_into_a_flagless_command() -> None:
    r = runner.invoke(app, ["action", "resolve", "k", "--took-effect", "--not-sent", "--json"])
    assert r.exit_code == 2
    assert json.loads(r.stdout)["ok"] is False
    r = runner.invoke(app, ["mock", "nope"])  # no --json flag: reset by the root callback
    assert r.exit_code == 2
    assert r.stdout == ""
    assert r.stderr.startswith("error: ")


def test_invalid_settings_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPPD_DRAIN_SECONDS", "soon")
    with pytest.raises(typer.Exit) as exc:
        load_settings()
    assert exc.value.exit_code == EXIT_USAGE


def test_invalid_settings_are_json_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPPD_DRAIN_SECONDS", "soon")
    r = runner.invoke(app, ["doctor", "--json"])
    assert r.exit_code == 2
    out = json.loads(r.stdout)
    assert out["ok"] is False
    assert "MAPPD_DRAIN_SECONDS" in out["error"]


def test_progress_and_status_do_nothing_without_a_tty() -> None:
    with progress("working", False) as tick:
        assert tick is None
    with progress("working", True) as tick:
        assert tick is None
    with status("working", False), status("working", True):
        pass


def test_table_pads_every_column_but_the_last() -> None:
    assert table(["A", "BB"], [["1", "22"], ["333", "4"]]) == [
        "A    BB",
        "1    22",
        "333  4",
    ]


def test_table_prints_the_header_only_when_there_are_rows() -> None:
    assert table(["A", "B"], []) == []
