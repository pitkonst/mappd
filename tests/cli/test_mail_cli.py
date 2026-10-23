import json
import sys
from typing import TYPE_CHECKING
from uuid import uuid4

import dbos_async
import pytest
from fake_mail import FakeSmtp, install
from typer.testing import CliRunner

from mappd import mail
from mappd.channels.email import MailError
from mappd.cli import app
from mappd.process import register_process

if TYPE_CHECKING:
    from pathlib import Path

    from mappd.settings import Settings

runner = CliRunner()
SMTPS: dict[str, FakeSmtp] = {}

PROCESS = """
from mappd.process import Mailbox, Process


async def receive(email):
    return None


process = Process(name="{name}", mailboxes=(Mailbox(name="{name}", receive=receive),))
"""


def processes_with(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / f"procs_{uuid4().hex}"
    for name in names:
        (root / name).mkdir(parents=True)
        (root / name / "__init__.py").write_text(PROCESS.format(name=name))
        register_process(root, name)
    return root


@pytest.fixture
def env(migrated: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Two mailboxes in a throwaway processes package: `inbox_<tag>` (imap) and `quiet_<tag>`."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    tag = uuid4().hex[:6]
    on, off = f"inbox_{tag}", f"quiet_{tag}"
    monkeypatch.setenv("MAPPD_DATABASE_URL", migrated.database_url)
    monkeypatch.setenv("MAPPD_PROCESSES_DIR", str(processes_with(tmp_path, on, off)))
    monkeypatch.delenv(f"MAPPD_{off.upper()}_MAIL_MODE", raising=False)
    SMTPS[tag] = install(monkeypatch, on)[1]
    return tag


def test_mail_status_lists_mailboxes_and_cursors(env: str, dbos_runtime: Settings) -> None:
    del dbos_runtime
    on = f"inbox_{env}"
    dbos_async.run(mail.save_cursor(on, 7, 41))
    r = runner.invoke(app, ["mail", "status", "--json"])
    assert r.exit_code == 0, r.stderr
    boxes = {b["name"]: b for b in json.loads(r.stdout)["mailboxes"]}
    assert boxes[on]["mode"] == "imap"
    assert (boxes[on]["uidvalidity"], boxes[on]["last_uid"]) == (7, 41)
    assert boxes[on]["polled_at"] is not None


def test_mail_status_empty_says_no_mailboxes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    empty = tmp_path / "empty_procs"
    empty.mkdir()
    monkeypatch.setenv("MAPPD_PROCESSES_DIR", str(empty))
    r = runner.invoke(app, ["mail", "status"])
    assert r.exit_code == 0
    assert r.stdout.strip() == "no mailboxes"


def test_mail_status_reports_off_mailboxes(env: str) -> None:
    r = runner.invoke(app, ["mail", "status", "--json"])
    assert r.exit_code == 0, r.stderr
    boxes = {b["name"]: b for b in json.loads(r.stdout)["mailboxes"]}
    quiet = boxes[f"quiet_{env}"]
    assert (quiet["mode"], quiet["last_uid"]) == ("off", None)
    plain = runner.invoke(app, ["mail", "status"])
    assert f"quiet_{env}" in plain.stdout


def sent(env: str) -> FakeSmtp:
    return SMTPS[env]


def test_mail_inject_sends_to_the_mailbox_address(env: str) -> None:
    r = runner.invoke(
        app,
        [
            "mail",
            "inject",
            f"inbox_{env}",
            "--from",
            "customer@mappd.test",
            "--subject",
            "Sensor offline",
            "--body",
            "Room 3 is dark.",
            "--in-reply-to",
            "<earlier@mappd.test>",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.stderr
    (message,) = sent(env).sent
    assert message["To"] == f"inbox_{env}@mappd.test"
    assert message["From"] == "customer@mappd.test"
    assert message["In-Reply-To"] == "<earlier@mappd.test>"
    assert json.loads(r.stdout)["message_id"] == message["Message-ID"]


def test_mail_inject_unknown_mailbox_exits_2(env: str) -> None:
    del env
    r = runner.invoke(app, ["mail", "inject", "nobody", "--from", "a@b.c", "--subject", "s"])
    assert r.exit_code == 2
    assert "nobody" in r.stderr


def test_mail_inject_into_an_off_mailbox_exits_2(env: str) -> None:
    r = runner.invoke(app, ["mail", "inject", f"quiet_{env}", "--from", "a@b.c", "--subject", "s"])
    assert r.exit_code == 2
    assert "MAIL_MODE" in r.stderr


def test_mail_inject_reports_smtp_errors(env: str) -> None:
    sent(env).fail_with = MailError("SMTP smtp.mappd.test:587: refused")
    r = runner.invoke(app, ["mail", "inject", f"inbox_{env}", "--from", "a@b.c", "--subject", "s"])
    assert r.exit_code == 1
    assert "refused" in r.stderr


def test_mail_help_has_examples() -> None:
    for command in ("status", "inject"):
        r = runner.invoke(app, ["mail", command, "--help"])
        assert r.exit_code == 0
        assert "mappd mail" in r.stdout
