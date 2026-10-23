import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import dbos_async
import psycopg
import pytest
from fake_mail import FakeMailbox, install, raw

from mappd import mail, telemetry
from mappd.actions import OutcomeUnknownError, resolve
from mappd.channels.email import (
    Email,
    ReplyContext,
    SmtpRejectedError,
    message_id_for,
    parse,
)
from mappd.db import connect
from mappd.process import Mailbox, Process

if TYPE_CHECKING:
    from pathlib import Path

    from mappd.settings import Settings


async def ignore(_email: Email) -> None:
    return None


def test_register_schedules_imap_mailboxes_only(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, "support")
    monkeypatch.setenv("MAPPD_MAIL_POLL_SECONDS", "5")
    monkeypatch.delenv("MAPPD_RFQ_MAIL_MODE", raising=False)
    processes = [
        Process(name="support", mailboxes=(Mailbox(name="support", receive=ignore),)),
        Process(name="rfq", mailboxes=(Mailbox(name="rfq", receive=ignore),)),
    ]
    (schedule,) = mail.register(processes)
    assert (schedule.name, schedule.cron, schedule.context) == (
        "support.mail",
        "*/5 * * * * *",
        "support",
    )
    assert schedule.workflow is mail.mail_poll
    assert set(mail.MAILBOXES) == {"support", "rfq"}


def test_duplicate_mailbox_names_are_refused() -> None:
    box = Mailbox(name="support", receive=ignore)
    with pytest.raises(ValueError, match="support"):
        mail.register([Process(name="a", mailboxes=(box,)), Process(name="b", mailboxes=(box,))])


def test_mailbox_names_are_identifiers() -> None:
    with pytest.raises(ValueError, match="identifier"):
        Mailbox(name="sup-port", receive=ignore)


@pytest.mark.parametrize("value", ["0", "60", "soon"])
def test_poll_interval_comes_from_the_env(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MAPPD_MAIL_POLL_SECONDS", value)
    with pytest.raises(ValueError, match="MAPPD_MAIL_POLL_SECONDS"):
        mail.poll_seconds()


def test_poll_interval_defaults_to_thirty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAPPD_MAIL_POLL_SECONDS", raising=False)
    assert mail.poll_seconds() == 30


class Inbox:
    """A process's receive hook: records every call, keeps one case per Message-ID."""

    def __init__(self, fail: dict[str, BaseException] | None = None) -> None:
        self.calls: list[Email] = []
        self.cases: dict[str, Email] = {}
        self.fail = fail or {}

    async def __call__(self, email: Email) -> None:
        self.calls.append(email)
        if error := self.fail.get(email.message_id):
            raise error
        self.cases.setdefault(email.message_id, email)


@pytest.fixture
def mailbox(
    dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, FakeMailbox, Inbox]:
    """A fresh mailbox name (cursors persist in the session DB) with a fake folder and hook."""
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    name = f"box{uuid4().hex[:8]}"
    box, _ = install(monkeypatch, name, backfill=True)
    inbox = Inbox()
    monkeypatch.setitem(mail.MAILBOXES, name, Mailbox(name=name, receive=inbox))
    return name, box, inbox


def tick(name: str) -> None:
    dbos_async.run(mail.mail_poll(datetime.now(UTC), name))


def test_new_mail_reaches_receive_once(mailbox: tuple[str, FakeMailbox, Inbox]) -> None:
    name, box, inbox = mailbox
    box.deliver(raw("<a@example.com>"))
    box.deliver(raw("<b@example.com>"))
    tick(name)
    tick(name)
    assert [e.message_id for e in inbox.calls] == ["<a@example.com>", "<b@example.com>"]
    cursor = mail.cursor(name)
    assert cursor is not None
    assert (cursor.uidvalidity, cursor.last_uid) == (1, 2)
    assert cursor.polled_at is not None


def test_first_run_skips_existing_mail(
    mailbox: tuple[str, FakeMailbox, Inbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    name, box, inbox = mailbox
    monkeypatch.setenv(f"MAPPD_{name.upper()}_IMAP_BACKFILL", "false")
    box.deliver(raw("<old@example.com>"))
    tick(name)
    assert inbox.calls == []
    box.deliver(raw("<new@example.com>"))
    tick(name)
    assert [e.message_id for e in inbox.calls] == ["<new@example.com>"]


def test_backfill_reads_existing_mail(mailbox: tuple[str, FakeMailbox, Inbox]) -> None:
    name, box, inbox = mailbox
    box.deliver(raw("<old@example.com>"))
    tick(name)
    assert [e.message_id for e in inbox.calls] == ["<old@example.com>"]


def test_crash_before_the_cursor_is_saved_rereads_without_duplicates(
    mailbox: tuple[str, FakeMailbox, Inbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    name, box, inbox = mailbox
    box.deliver(raw("<a@example.com>"))
    real = mail.save_cursor

    async def crash(*_args: object) -> None:
        raise RuntimeError("worker died")

    monkeypatch.setattr(mail, "save_cursor", crash)
    with pytest.raises(RuntimeError, match="worker died"):
        tick(name)
    monkeypatch.setattr(mail, "save_cursor", real)
    tick(name)
    assert len(inbox.calls) == 2, "the batch is read again after the crash"
    assert list(inbox.cases) == ["<a@example.com>"], "the Message-ID collapses the repeat"


def test_uidvalidity_change_rereads_without_duplicates(
    mailbox: tuple[str, FakeMailbox, Inbox],
) -> None:
    name, box, inbox = mailbox
    box.deliver(raw("<a@example.com>"))
    tick(name)
    box.recreate()
    tick(name)
    assert list(inbox.cases) == ["<a@example.com>"]
    cursor = mail.cursor(name)
    assert cursor is not None
    assert (cursor.uidvalidity, cursor.last_uid) == (2, 1)


def test_automated_mail_is_skipped_and_the_cursor_advances(
    mailbox: tuple[str, FakeMailbox, Inbox], caplog: pytest.LogCaptureFixture
) -> None:
    name, box, inbox = mailbox
    box.deliver(raw("<auto@example.com>", headers="Auto-Submitted: auto-replied"))
    box.deliver(raw("<own@example.com>", sender=f"{name}@mappd.test"))
    with caplog.at_level(logging.INFO, logger="mappd.mail"):
        tick(name)
    assert inbox.calls == []
    assert "mail.skipped" in caplog.text
    cursor = mail.cursor(name)
    assert cursor is not None
    assert cursor.last_uid == 2


def test_a_poison_message_does_not_block_the_mailbox(
    mailbox: tuple[str, FakeMailbox, Inbox], caplog: pytest.LogCaptureFixture
) -> None:
    name, box, inbox = mailbox
    inbox.fail["<bad@example.com>"] = ValueError("sender is empty")
    for mid in ("<one@example.com>", "<bad@example.com>", "<three@example.com>"):
        box.deliver(raw(mid))
    with caplog.at_level(logging.WARNING, logger="mappd.mail"):
        tick(name)
    assert list(inbox.cases) == ["<one@example.com>", "<three@example.com>"]
    assert "mail.rejected" in caplog.text
    assert "<bad@example.com>" in caplog.text
    cursor = mail.cursor(name)
    assert cursor is not None
    assert cursor.last_uid == 3


def test_an_infrastructure_error_fails_the_tick_and_keeps_the_cursor(
    mailbox: tuple[str, FakeMailbox, Inbox],
) -> None:
    name, box, inbox = mailbox
    box.deliver(raw("<a@example.com>"))
    tick(name)
    box.deliver(raw("<b@example.com>"))
    inbox.fail["<b@example.com>"] = RuntimeError("database down")
    with pytest.raises(RuntimeError, match="database down"):
        tick(name)
    cursor = mail.cursor(name)
    assert cursor is not None
    assert cursor.last_uid == 1
    del inbox.fail["<b@example.com>"]
    tick(name)
    assert list(inbox.cases) == ["<a@example.com>", "<b@example.com>"]


def test_a_burst_is_read_over_several_ticks(mailbox: tuple[str, FakeMailbox, Inbox]) -> None:
    name, box, inbox = mailbox
    for i in range(120):
        box.deliver(raw(f"<m{i:03}@example.com>"))
    seen = []
    for _ in range(3):
        tick(name)
        seen.append(len(inbox.calls))
    assert seen == [50, 100, 120]
    assert [e.message_id for e in inbox.calls] == [f"<m{i:03}@example.com>" for i in range(120)]


def test_a_tick_for_an_unknown_mailbox_does_nothing(dbos_runtime: Settings) -> None:
    del dbos_runtime
    tick("nobody")  # a schedule left over from a removed process: no error


@pytest.fixture
def outbox(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    return dbos_runtime


def reply() -> ReplyContext:
    return ReplyContext(
        message_id="<orig@example.com>",
        references=[],
        subject="Sensor offline",
        sender="anna@example.com",
        text="It is offline.",
    )


def action_row(settings: Settings, key: str) -> tuple[str, int, str | None]:
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            "SELECT status, attempts, last_error FROM actions WHERE idempotency_key = %s", (key,)
        ).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def test_send_threads_and_returns_the_message_id(
    outbox: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, smtp = install(monkeypatch, "support")
    ext = f"t-{uuid4().hex[:8]}"
    mid = dbos_async.run(
        mail.send(
            ext,
            mailbox="support",
            to="anna@example.com",
            subject="Sensor offline",
            text="Restart it.",
            reply_to=reply(),
        )
    )
    (sent,) = smtp.sent
    assert sent["Message-ID"] == mid
    assert mid.endswith("@mappd.test>")
    assert sent["In-Reply-To"] == "<orig@example.com>"
    assert sent["Subject"] == "Re: Sensor offline"
    assert (sent["From"], sent["To"]) == ("support@mappd.test", "anna@example.com")
    assert action_row(outbox, f"mappd.send_email:support:{ext}")[0] == "done"


def test_send_twice_sends_once(outbox: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _, smtp = install(monkeypatch, "support")
    ext = f"t-{uuid4().hex[:8]}"

    async def twice() -> tuple[str, str]:
        first = await mail.send(
            ext, mailbox="support", to="a@example.com", subject="S", text="T", reply_to=None
        )
        second = await mail.send(
            ext, mailbox="support", to="a@example.com", subject="S", text="T", reply_to=None
        )
        return first, second

    first, second = dbos_async.run(twice())
    assert first == second
    assert len(smtp.sent) == 1


def test_send_in_off_mode_fails_with_mail_off(
    outbox: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAPPD_SUPPORT_MAIL_MODE", raising=False)
    ext = f"t-{uuid4().hex[:8]}"
    with pytest.raises(mail.MailOffError, match="MAPPD_SUPPORT_MAIL_MODE=imap"):
        dbos_async.run(
            mail.send(
                ext, mailbox="support", to="a@example.com", subject="S", text="T", reply_to=None
            )
        )
    assert action_row(outbox, f"mappd.send_email:support:{ext}")[:2] == ("failed", 1)


def test_send_after_data_unknown_escalates(
    outbox: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, smtp = install(monkeypatch, "support")
    smtp.fail_with = OutcomeUnknownError("SMTP smtp.mappd.test:587: timeout after the data")
    ext = f"t-{uuid4().hex[:8]}"

    def send() -> str:
        return dbos_async.run(
            mail.send(
                ext, mailbox="support", to="a@example.com", subject="S", text="T", reply_to=None
            )
        )

    with pytest.raises(OutcomeUnknownError):
        send()
    assert action_row(outbox, f"mappd.send_email:support:{ext}")[0] == "unknown"
    smtp.fail_with = None
    with pytest.raises(OutcomeUnknownError):
        send()
    assert smtp.attempts == 1, "an unknown send is never resent by itself"


def test_rejected_recipient_fails_without_retry(
    outbox: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, smtp = install(monkeypatch, "support")
    smtp.fail_with = SmtpRejectedError("SMTP smtp.mappd.test:587: 550 no such user")
    ext = f"t-{uuid4().hex[:8]}"
    with pytest.raises(SmtpRejectedError):
        dbos_async.run(
            mail.send(
                ext, mailbox="support", to="a@example.com", subject="S", text="T", reply_to=None
            )
        )
    status, attempts, error = action_row(outbox, f"mappd.send_email:support:{ext}")
    assert (status, attempts, smtp.attempts) == ("failed", 1, 1)
    assert error is not None
    assert "550" in error


def test_poll_interval_reads_the_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / "poll.env"
    dotenv.write_text("MAPPD_MAIL_POLL_SECONDS=7\n")
    monkeypatch.delenv("MAPPD_MAIL_POLL_SECONDS", raising=False)
    monkeypatch.setenv("MAPPD_ENV_FILE", str(dotenv))
    assert mail.poll_seconds() == 7


def test_an_unparseable_message_is_skipped(
    mailbox: tuple[str, FakeMailbox, Inbox],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    name, box, inbox = mailbox
    real = parse

    def fragile(raw_bytes: bytes, **kwargs: object) -> Email:
        if b"BOOM" in raw_bytes:
            raise RuntimeError("parser bug")
        return real(raw_bytes, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("mappd.mail.parse", fragile)
    monkeypatch.setattr("mappd.mail.parse_headers", fragile)
    box.deliver(raw("<one@example.com>"))
    box.deliver(raw("<bad@example.com>", body="BOOM"))
    box.deliver(raw("<three@example.com>"))
    with caplog.at_level(logging.INFO, logger="mappd.mail"):
        tick(name)
    assert list(inbox.cases) == ["<one@example.com>", "<three@example.com>"]
    assert "unparseable" in caplog.text
    cursor = mail.cursor(name)
    assert cursor is not None
    assert cursor.last_uid == 3


def test_a_database_data_error_rejects_one_message(
    mailbox: tuple[str, FakeMailbox, Inbox], caplog: pytest.LogCaptureFixture
) -> None:
    name, box, inbox = mailbox
    inbox.fail["<bad@example.com>"] = psycopg.DataError("text fields cannot contain NUL")
    box.deliver(raw("<bad@example.com>"))
    box.deliver(raw("<good@example.com>"))
    with caplog.at_level(logging.WARNING, logger="mappd.mail"):
        tick(name)
    assert list(inbox.cases) == ["<good@example.com>"]
    assert "mail.rejected" in caplog.text


def test_uidvalidity_change_without_backfill_skips_old_mail(
    mailbox: tuple[str, FakeMailbox, Inbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    name, box, inbox = mailbox
    monkeypatch.setenv(f"MAPPD_{name.upper()}_IMAP_BACKFILL", "false")
    box.deliver(raw("<history@example.com>"))
    tick(name)
    box.deliver(raw("<new@example.com>"))
    tick(name)
    box.recreate()
    tick(name)
    assert list(inbox.cases) == ["<new@example.com>"], "the old history stays old"
    box.deliver(raw("<later@example.com>"))
    tick(name)
    assert list(inbox.cases) == ["<new@example.com>", "<later@example.com>"]


def test_a_send_that_failed_while_off_goes_out_once_mail_is_on(
    outbox: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAPPD_SUPPORT_MAIL_MODE", raising=False)
    ext = f"t-{uuid4().hex[:8]}"

    def send() -> str:
        return dbos_async.run(
            mail.send(
                ext, mailbox="support", to="a@example.com", subject="S", text="T", reply_to=None
            )
        )

    with pytest.raises(mail.MailOffError):
        send()
    _, smtp = install(monkeypatch, "support")
    monkeypatch.setenv("MAPPD_SUPPORT_MAIL_FROM", "support@acme.example")
    mid = send()
    (sent,) = smtp.sent
    assert sent["Message-ID"] == mid
    assert mid.endswith("@acme.example>")


def test_a_settled_send_still_reports_its_message_id(
    outbox: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, smtp = install(monkeypatch, "support")
    smtp.fail_with = OutcomeUnknownError("timeout after the data")
    ext = f"t-{uuid4().hex[:8]}"
    key = f"mappd.send_email:support:{ext}"

    def send() -> str:
        return dbos_async.run(
            mail.send(
                ext, mailbox="support", to="a@example.com", subject="S", text="T", reply_to=None
            )
        )

    with pytest.raises(OutcomeUnknownError):
        send()
    with connect(outbox) as conn:
        resolve(conn, key, took_effect=True, operator="olga")
    assert send() == message_id_for(key, "support@mappd.test")


def test_a_tick_without_mail_marks_its_trace_idle(
    mailbox: tuple[str, FakeMailbox, Inbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    name, box, _ = mailbox
    idle: list[bool] = []
    monkeypatch.setattr(telemetry, "idle", lambda: idle.append(True))
    box.deliver(raw("<c@example.com>"))
    tick(name)
    assert idle == [], "a tick that read mail is traced"
    tick(name)
    assert idle == [True]
