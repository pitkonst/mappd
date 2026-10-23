"""In-memory mailboxes and SMTP servers for tests of `mappd.mail` and the processes on it.

`install(monkeypatch, "support")` turns mailbox `support` on (`MAPPD_SUPPORT_MAIL_MODE=imap`)
and routes its IMAP and SMTP clients here; tests deliver raw mail into the `FakeMailbox` and
read what was sent from `FakeSmtp.sent`.
"""

from typing import TYPE_CHECKING

from mappd import mail
from mappd.channels.email import Fetched, FetchedMessage, MailConfig

if TYPE_CHECKING:
    from email.message import EmailMessage

    import pytest

PASSWORD = "fake-password"  # noqa: S105 - a test value


class FakeMailbox:
    """One folder: UIDs in delivery order, a UIDVALIDITY a test can change."""

    def __init__(self, uidvalidity: int = 1) -> None:
        self.uidvalidity = uidvalidity
        self.messages: dict[int, bytes] = {}
        self.next_uid = 1
        self.fetches = 0

    def deliver(self, raw: bytes) -> int:
        """Add a message; returns its UID."""
        uid = self.next_uid
        self.messages[uid] = raw
        self.next_uid += 1
        return uid

    def recreate(self) -> None:
        """The folder was recreated: a new UIDVALIDITY, the same messages under new UIDs."""
        self.uidvalidity += 1
        old = list(self.messages.values())
        self.messages, self.next_uid = {}, 1
        for raw in old:
            self.deliver(raw)

    def fetch_since(
        self,
        uidvalidity: int | None,
        last_uid: int,
        *,
        limit: int = 50,
        start_at_end: bool = False,
        reset_to_end: bool = False,
    ) -> Fetched:
        self.fetches += 1
        if uidvalidity is not None and uidvalidity != self.uidvalidity:
            last_uid = 0
            start_at_end = start_at_end or reset_to_end
        found = (
            []
            if start_at_end
            else [
                FetchedMessage(uid=uid, size=len(raw), headers=raw.split(b"\r\n\r\n")[0], raw=raw)
                for uid, raw in sorted(self.messages.items())
                if uid > last_uid
            ][:limit]
        )
        return Fetched(uidvalidity=self.uidvalidity, uidnext=self.next_uid, messages=found)

    def check(self) -> str:
        return f"INBOX, {len(self.messages)} messages"


class FakeSmtp:
    """Records each sent message; `fail_with` makes the next sends raise instead."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_with: BaseException | None = None
        self.attempts = 0

    def send(self, message: EmailMessage) -> None:
        self.attempts += 1
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)

    def check(self) -> str:
        return "smtp ok"


def env(name: str) -> dict[str, str]:
    """The minimal settings of an `imap` mailbox called `name`."""
    prefix = f"MAPPD_{name.upper()}_"
    return {
        f"{prefix}MAIL_MODE": "imap",
        f"{prefix}MAIL_FROM": f"{name}@mappd.test",
        f"{prefix}IMAP_HOST": "imap.mappd.test",
        f"{prefix}IMAP_USER": f"{name}@mappd.test",
        f"{prefix}IMAP_PASSWORD": PASSWORD,
        f"{prefix}SMTP_HOST": "smtp.mappd.test",
    }


_BOXES: dict[str, FakeMailbox] = {}
_SMTPS: dict[str, FakeSmtp] = {}


def install(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    mailbox: FakeMailbox | None = None,
    smtp: FakeSmtp | None = None,
    backfill: bool = False,
) -> tuple[FakeMailbox, FakeSmtp]:
    """Turn mailbox `name` on with fake servers; returns them."""
    for key, value in env(name).items():
        monkeypatch.setenv(key, value)
    if backfill:
        monkeypatch.setenv(f"MAPPD_{name.upper()}_IMAP_BACKFILL", "true")
    box, server = mailbox or FakeMailbox(), smtp or FakeSmtp()
    monkeypatch.setitem(_BOXES, name, box)
    monkeypatch.setitem(_SMTPS, name, server)

    def imap(config: MailConfig) -> FakeMailbox:
        return _BOXES[config.name]

    def smtp_client(config: MailConfig) -> FakeSmtp:
        return _SMTPS[config.name]

    monkeypatch.setattr(mail, "imap_client", imap)
    monkeypatch.setattr(mail, "smtp_client", smtp_client)
    return box, server


def raw(
    message_id: str,
    *,
    sender: str = "anna@example.com",
    to: str = "support@mappd.test",
    subject: str = "Sensor offline",
    body: str = "The sensor in room 3 is offline.",
    headers: str = "",
) -> bytes:
    """A small synthetic email."""
    extra = "".join(f"{line}\r\n" for line in headers.splitlines() if line)
    return (
        f"From: {sender}\r\nTo: {to}\r\nSubject: {subject}\r\nMessage-ID: {message_id}\r\n"
        f"{extra}Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()
