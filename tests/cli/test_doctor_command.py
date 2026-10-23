import json

import pytest
from typer.testing import CliRunner

from mappd import doctor
from mappd.cli import app
from mappd.doctor import Check

runner = CliRunner()


@pytest.fixture
def down(monkeypatch: pytest.MonkeyPatch) -> None:
    # Port 9 (discard) is closed on any sane test machine.
    monkeypatch.setenv("MAPPD_DATABASE_URL", "postgresql://u:secret@127.0.0.1:9/db")
    monkeypatch.setenv("MAPPD_SYSTEM_DATABASE_URL", "postgresql://u:secret@127.0.0.1:9/sys")
    monkeypatch.setenv("MAPPD_GATEWAY_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("MAPPD_MLFLOW_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MAPPD_PROCESSES_DIR", "/nonexistent")


def test_doctor_json_reports_every_layer_and_exits_1(down: None) -> None:
    r = runner.invoke(app, ["doctor", "--json"])
    assert r.exit_code == 1
    out = json.loads(r.stdout)
    assert out["ok"] is False
    assert [c["layer"] for c in out["checks"]] == ["M", "A", "P", "P", "P", "P", "P", "·", "D"]
    assert set(out["checks"][0]) == {
        "layer",
        "group",
        "name",
        "status",
        "ok",
        "target",
        "detail",
        "fix",
    }
    assert out["summary"]["fail"] >= 1


def test_doctor_never_prints_credentials(down: None) -> None:
    for args in (["doctor"], ["doctor", "--json"]):
        r = runner.invoke(app, args)
        assert "secret" not in r.stdout
        assert "secret" not in r.stderr


def test_doctor_exits_2_on_invalid_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPPD_DRAIN_SECONDS", "soon")
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 2
    assert "MAPPD_DRAIN_SECONDS" in r.stderr


CHECKS = [
    Check("M", "gateway", "gateway", "ok", "http://gw/v1", "8 models"),
    Check("A", "agents", "agents", "ok", None, "5 agents in 3 processes"),
    Check("P", "processes", "processes", "ok", "processes", "installation, rfq, support"),
    Check("P", "processes", "mail:rfq", "off", None, "polling off (MAPPD_RFQ_MAIL_MODE=imap)"),
    Check("·", "traces", "mlflow", "skip", "http://ml", "skipped in the healthcheck"),
    Check(
        "D",
        "workflows",
        "workflows",
        "fail",
        "postgresql://db/sys",
        "2 stranded on version a1b2c3d",
        "mappd workflow recover (or /workflows?status=stranded)",
    ),
]


def _fixed(monkeypatch: pytest.MonkeyPatch, checks: list[Check]) -> None:
    monkeypatch.setattr(doctor, "run_all", lambda _s, health=False: checks)


def test_doctor_groups_checks_under_layer_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed(monkeypatch, CHECKS)
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 1
    assert r.stdout.splitlines() == [
        "M  model gateway",
        "   ok    gateway    8 models                                http://gw/v1",
        "",
        "A  agents",
        "   ok    agents     5 agents in 3 processes",
        "",
        "P  processes",
        "   ok    processes  installation, rfq, support              processes",
        "   off   mail:rfq   polling off (MAPPD_RFQ_MAIL_MODE=imap)",
        "",
        "·  traces",
        "   skip  mlflow     skipped in the healthcheck              http://ml",
        "",
        "D  durable execution",
        "   FAIL  workflows  2 stranded on version a1b2c3d           postgresql://db/sys",
        "         fix: mappd workflow recover (or /workflows?status=stranded)",
        "",
        "3 ok, 1 off, 1 skipped, 1 failed",
    ]


def test_doctor_passes_with_only_ok_and_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed(
        monkeypatch,
        [
            Check("M", "gateway", "gateway", "ok", "http://gw/v1", "8 models"),
            Check("P", "processes", "mail:x", "off", None, "polling off"),
        ],
    )
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0
    assert r.stdout.splitlines()[-1] == "1 ok, 1 off, 0 failed"
    assert "skipped" not in r.stdout


def test_doctor_json_summary_and_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed(monkeypatch, CHECKS)
    r = runner.invoke(app, ["doctor", "--json"])
    assert r.exit_code == 1
    out = json.loads(r.stdout)
    assert out["summary"] == {"ok": 3, "off": 1, "skip": 1, "fail": 1}
    stranded = out["checks"][-1]
    assert stranded["status"] == "fail"
    assert stranded["ok"] is False
    assert stranded["fix"] == "mappd workflow recover (or /workflows?status=stranded)"


def test_doctor_plain_output_has_no_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed(monkeypatch, CHECKS)
    assert "\x1b[" not in runner.invoke(app, ["doctor"]).stdout


def test_doctor_colours_only_on_a_terminal_and_without_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed(monkeypatch, CHECKS)
    assert "\x1b[" in runner.invoke(app, ["doctor"], color=True).stdout
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b[" not in runner.invoke(app, ["doctor"], color=True).stdout
