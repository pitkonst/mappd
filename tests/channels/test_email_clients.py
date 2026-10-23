import imaplib
import smtplib
from typing import TYPE_CHECKING, Any

import pytest

from mappd.actions import OutcomeUnknownError, PermanentError
from mappd.channels import email as mail

if TYPE_CHECKING:
    import ssl
    from email.message import EmailMessage
    from pathlib import Path

ENV = {
    "MAPPD_SUPPORT_MAIL_MODE": "imap",
    "MAPPD_SUPPORT_MAIL_FROM": "support@mappd.test",
    "MAPPD_SUPPORT_IMAP_HOST": "imap.mappd.test",
    "MAPPD_SUPPORT_IMAP_USER": "support@mappd.test",
    "MAPPD_SUPPORT_IMAP_PASSWORD": "pw-s3cret",
    "MAPPD_SUPPORT_SMTP_HOST": "smtp.mappd.test",
    "MAPPD_SUPPORT_SMTP_USER": "support@mappd.test",
    "MAPPD_SUPPORT_SMTP_PASSWORD": "pw-s3cret",
}


def config(**overrides: str) -> mail.MailConfig:
    return mail.MailConfig.from_env("support", {**ENV, **overrides})


def raw_mail(uid: int) -> bytes:
    return (
        f"From: anna@example.com\r\nTo: support@mappd.test\r\nSubject: n{uid}\r\n"
        f"Message-ID: <m{uid}@example.com>\r\n\r\nbody {uid}\r\n"
    ).encode()


class FakeImap:
    """Answers like imaplib.IMAP4 for the calls ImapClient makes; records every call."""

    error = imaplib.IMAP4.error  # kept when a test replaces imaplib.IMAP4 with a subclass

    def __init__(
        self, uids: list[int], *, uidvalidity: int = 7, sizes: dict[int, int] | None = None
    ):
        self.uids = uids
        self.uidvalidity = uidvalidity
        self.sizes = sizes or {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.login_error: str | None = None

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", (user,)))
        if self.login_error:
            raise imaplib.IMAP4.error(self.login_error)
        return "OK", [b"logged in"]

    def select(self, folder: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", (folder, readonly)))
        return "OK", [str(len(self.uids)).encode()]

    def response(self, code: str) -> tuple[str, list[bytes | None]]:
        values = {"UIDVALIDITY": self.uidvalidity, "UIDNEXT": max(self.uids, default=0) + 1}
        return code, [str(values[code]).encode()]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append((command.upper(), args))
        if command.upper() == "SEARCH":
            low = int(args[0].split()[1].split(":")[0])
            found = [u for u in self.uids if u >= low] or self.uids[-1:]  # like a real server
            return "OK", [" ".join(map(str, found)).encode()]
        uid, what = int(args[0]), args[1]
        size = self.sizes.get(uid, len(raw_mail(uid)))
        if "HEADER" in what:
            meta = f"1 (UID {uid} RFC822.SIZE {size} BODY[HEADER] {{10}}".encode()
            return "OK", [(meta, raw_mail(uid).split(b"\r\n\r\n")[0] + b"\r\n\r\n"), b")"]
        return "OK", [(f"1 (UID {uid} BODY[] {{10}}".encode(), raw_mail(uid)), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout", ()))
        return "BYE", [b""]


class FakeSmtp:
    """Answers like smtplib.SMTP for the calls SmtpClient makes; failures are scripted."""

    def __init__(self, **fail: BaseException | tuple[int, bytes]) -> None:
        self.fail = fail
        self.calls: list[str] = []
        self.data_sent: bytes | None = None

    def _step(self, name: str) -> tuple[int, bytes] | None:
        self.calls.append(name)
        outcome = self.fail.get(name)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        return self._step("login") or (235, b"ok")

    def ehlo_or_helo_if_needed(self) -> None:
        self._step("ehlo")

    def mail(self, sender: str) -> tuple[int, bytes]:
        return self._step("mail") or (250, b"ok")

    def rcpt(self, recipient: str) -> tuple[int, bytes]:
        return self._step("rcpt") or (250, b"ok")

    def data(self, message: bytes) -> tuple[int, bytes]:
        self.data_sent = message
        return self._step("data") or (250, b"queued")

    def quit(self) -> tuple[int, bytes]:
        return self._step("quit") or (221, b"bye")


def message() -> EmailMessage:
    return mail.build(
        sender="support@mappd.test",
        to="anna@example.com",
        subject="Hello",
        text="Body",
        message_id="<a@mappd.test>",
        reply_to=None,
    )


def test_from_env_reads_a_mailbox() -> None:
    c = config(MAPPD_SUPPORT_IMAP_FOLDER="Support", MAPPD_SUPPORT_IMAP_BACKFILL="true")
    assert (c.mode, c.mail_from, c.imap_host, c.imap_folder) == (
        "imap",
        "support@mappd.test",
        "imap.mappd.test",
        "Support",
    )
    assert (c.imap_port, c.imap_tls, c.smtp_port, c.smtp_tls) == (993, "ssl", 587, "starttls")
    assert c.backfill is True
    assert c.imap_password.get_secret_value() == "pw-s3cret"


def test_ports_follow_the_tls_mode() -> None:
    c = config(MAPPD_SUPPORT_IMAP_TLS="none", MAPPD_SUPPORT_SMTP_TLS="ssl")
    assert (c.imap_port, c.smtp_port) == (143, 465)
    assert config(MAPPD_SUPPORT_SMTP_PORT="2525").smtp_port == 2525


def test_off_mode_needs_nothing() -> None:
    c = mail.MailConfig.from_env("support", {})
    assert c.mode == "off"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAPPD_SUPPORT_IMAP_HOST", ""),
        ("MAPPD_SUPPORT_MAIL_FROM", ""),
        ("MAPPD_SUPPORT_MAIL_MODE", "pop3"),
        ("MAPPD_SUPPORT_IMAP_TLS", "maybe"),
        ("MAPPD_SUPPORT_SMTP_PORT", "smtp"),
    ],
)
def test_missing_or_bad_setting_names_the_variable(name: str, value: str) -> None:
    with pytest.raises(mail.MailConfigError, match=name):
        config(**{name: value})


def client(fake: FakeImap, **overrides: str) -> mail.ImapClient:
    return mail.ImapClient(config(**overrides), factory=lambda _c: fake)  # type: ignore[arg-type,return-value]


def test_fetch_reads_only_with_examine_and_peek() -> None:
    fake = FakeImap([1, 2])
    client(fake).fetch_since(7, 0)
    commands = {name for name, _ in fake.calls}
    assert ("select", ("INBOX", True)) in fake.calls, "read-only select (EXAMINE)"
    assert not commands & {"STORE", "COPY", "MOVE", "EXPUNGE"}
    fetches = [args[1] for name, args in fake.calls if name == "FETCH"]
    assert fetches
    assert all("PEEK" in what for what in fetches)
    assert fake.calls[-1] == ("logout", ())


def test_fetch_returns_uids_above_the_cursor_in_order() -> None:
    fake = FakeImap([3, 5, 9])
    got = client(fake).fetch_since(7, 3)
    assert (got.uidvalidity, got.uidnext) == (7, 10)
    assert [m.uid for m in got.messages] == [5, 9]
    assert got.messages[0].raw == raw_mail(5)
    assert client(FakeImap([3, 5, 9])).fetch_since(7, 9).messages == []


def test_fetch_limits_the_batch() -> None:
    got = client(FakeImap(list(range(1, 121)))).fetch_since(7, 0, limit=50)
    assert [m.uid for m in got.messages] == list(range(1, 51))


def test_uidvalidity_change_resets_the_cursor() -> None:
    got = client(FakeImap([1, 2], uidvalidity=8)).fetch_since(7, 2)
    assert got.uidvalidity == 8
    assert [m.uid for m in got.messages] == [1, 2]


def test_start_at_end_reads_nothing_and_reports_uidnext() -> None:
    fake = FakeImap([1, 2, 3])
    got = client(fake).fetch_since(None, 0, start_at_end=True)
    assert (got.messages, got.uidnext, got.uidvalidity) == ([], 4, 7)
    assert not [c for c in fake.calls if c[0] == "FETCH"]


def test_oversize_message_fetches_headers_only() -> None:
    fake = FakeImap([1], sizes={1: 30_000_000})
    (m,) = client(fake).fetch_since(7, 0).messages
    assert (m.raw, m.size) == (None, 30_000_000)
    assert m.headers.startswith(b"From: anna@example.com")
    assert [args[1] for name, args in fake.calls if name == "FETCH"] == [
        "(RFC822.SIZE BODY.PEEK[HEADER])"
    ]


def test_imap_check_reports_the_folder() -> None:
    assert client(FakeImap([1, 2])).check() == "INBOX, 2 messages"


@pytest.mark.parametrize(
    ("tls", "cls", "starttls"),
    [("ssl", "IMAP4_SSL", False), ("starttls", "IMAP4", True), ("none", "IMAP4", False)],
)
def test_imap_tls_modes(
    monkeypatch: pytest.MonkeyPatch, tls: str, cls: str, starttls: bool
) -> None:
    made: list[tuple[str, str, int]] = []
    started: list[bool] = []

    def make(name: str) -> type:
        class Conn(FakeImap):
            def __init__(self, host: str, port: int, **kwargs: Any) -> None:
                super().__init__([1])
                made.append((name, host, port))

            def starttls(
                self, ssl_context: ssl.SSLContext | None = None
            ) -> tuple[str, list[bytes]]:
                started.append(True)
                return "OK", [b""]

        return Conn

    monkeypatch.setattr(imaplib, "IMAP4_SSL", make("IMAP4_SSL"))
    monkeypatch.setattr(imaplib, "IMAP4", make("IMAP4"))
    mail.ImapClient(config(MAPPD_SUPPORT_IMAP_TLS=tls)).check()
    assert made[0][0] == cls
    assert bool(started) is starttls


@pytest.mark.parametrize(
    ("tls", "cls", "starttls"),
    [("ssl", "SMTP_SSL", False), ("starttls", "SMTP", True), ("none", "SMTP", False)],
)
def test_smtp_tls_modes(
    monkeypatch: pytest.MonkeyPatch, tls: str, cls: str, starttls: bool
) -> None:
    made: list[str] = []
    started: list[bool] = []

    def make(name: str) -> type:
        class Conn(FakeSmtp):
            def __init__(self, host: str, port: int, **kwargs: Any) -> None:
                super().__init__()
                made.append(name)

            def ehlo(self) -> tuple[int, bytes]:
                return 250, b"hi"

            def starttls(self, context: ssl.SSLContext | None = None) -> tuple[int, bytes]:
                started.append(True)
                return 220, b"go"

        return Conn

    monkeypatch.setattr(smtplib, "SMTP_SSL", make("SMTP_SSL"))
    monkeypatch.setattr(smtplib, "SMTP", make("SMTP"))
    mail.SmtpClient(config(MAPPD_SUPPORT_SMTP_TLS=tls)).send(message())
    assert made == [cls]
    assert bool(started) is starttls


def smtp(fake: FakeSmtp) -> mail.SmtpClient:
    return mail.SmtpClient(config(), factory=lambda _c: fake)  # type: ignore[arg-type,return-value]


def test_smtp_send_transmits_the_message() -> None:
    fake = FakeSmtp()
    smtp(fake).send(message())
    assert fake.calls == ["login", "ehlo", "mail", "rcpt", "data", "quit"]
    assert fake.data_sent is not None
    assert b"Message-ID: <a@mappd.test>\r\n" in fake.data_sent


def refused() -> BaseException:
    return smtplib.SMTPDataError(554, b"policy rejection")


@pytest.mark.parametrize(
    ("fail", "error"),
    [
        ({"mail": (451, b"try later")}, mail.SmtpTemporaryError),
        ({"mail": (550, b"sender refused")}, mail.SmtpRejectedError),
        ({"rcpt": (550, b"no such user")}, mail.SmtpRejectedError),
        ({"rcpt": (452, b"mailbox full")}, mail.SmtpTemporaryError),
        ({"data": smtplib.SMTPDataError(451, b"later")}, mail.SmtpTemporaryError),
        ({"data": refused()}, mail.SmtpRejectedError),
        ({"data": smtplib.SMTPServerDisconnected("gone")}, OutcomeUnknownError),
        ({"data": TimeoutError("timed out")}, OutcomeUnknownError),
        (
            {"login": smtplib.SMTPAuthenticationError(535, b"bad credentials")},
            mail.SmtpRejectedError,
        ),
        ({"ehlo": smtplib.SMTPServerDisconnected("gone")}, mail.SmtpTemporaryError),
    ],
)
def test_smtp_send_classification(
    fail: dict[str, BaseException | tuple[int, bytes]], error: type[BaseException]
) -> None:
    with pytest.raises(error):
        smtp(FakeSmtp(**fail)).send(message())


def test_smtp_rejection_carries_the_reply_and_is_permanent() -> None:
    with pytest.raises(mail.SmtpRejectedError, match=r"550 no such user") as caught:
        smtp(FakeSmtp(rcpt=(550, b"no such user"))).send(message())
    assert isinstance(caught.value, PermanentError)


def test_smtp_connection_refused_is_temporary() -> None:
    def refuse(_c: mail.MailConfig) -> smtplib.SMTP:
        raise ConnectionRefusedError(61, "Connection refused")

    with pytest.raises(mail.SmtpTemporaryError, match=r"smtp\.mappd\.test:587"):
        mail.SmtpClient(config(), factory=refuse).send(message())


def test_a_quit_failure_after_sending_is_ignored() -> None:
    smtp(FakeSmtp(quit=smtplib.SMTPServerDisconnected("gone"))).send(message())


def test_errors_never_contain_the_password() -> None:
    fake = FakeImap([1])
    fake.login_error = "AUTHENTICATIONFAILED pw-s3cret rejected"
    with pytest.raises(mail.MailError) as imap_error:
        client(fake).check()
    assert "pw-s3cret" not in str(imap_error.value)
    with pytest.raises(mail.SmtpRejectedError) as smtp_error:
        smtp(FakeSmtp(login=smtplib.SMTPAuthenticationError(535, b"pw-s3cret bad"))).send(message())
    assert "pw-s3cret" not in str(smtp_error.value)
    assert "pw-s3cret" not in repr(config())


def test_from_env_reads_the_dotenv_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dotenv = tmp_path / "mail.env"
    dotenv.write_text("".join(f"{k}={v}\n" for k, v in ENV.items()))
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MAPPD_ENV_FILE", str(dotenv))
    c = mail.MailConfig.from_env("support")
    assert (c.mode, c.imap_host) == ("imap", "imap.mappd.test")


def test_a_failed_login_closes_the_connection() -> None:
    fake = FakeImap([1])
    fake.login_error = "AUTHENTICATIONFAILED"
    with pytest.raises(mail.MailError):
        client(fake).check()
    assert fake.calls[-1] == ("logout", ()), "the socket is not left open"


def test_a_failed_smtp_login_closes_the_connection() -> None:
    fake = FakeSmtp(login=smtplib.SMTPAuthenticationError(535, b"bad credentials"))
    with pytest.raises(mail.SmtpRejectedError):
        smtp(fake).send(message())
    assert fake.calls[-1] == "quit", "the socket is not left open"


@pytest.mark.parametrize(
    ("mail_from", "name"),
    [
        ("Support <support@mappd.test>", "mappd.test"),
        ("rfq@example.com", "example.com"),
        ("", "localhost"),
    ],
)
def test_smtp_greets_with_the_senders_domain(mail_from: str, name: str) -> None:
    assert mail.helo_name(mail.MailConfig(name="support", mail_from=mail_from)) == name


@pytest.mark.parametrize("tls", ["ssl", "starttls", "none"])
def test_smtp_never_looks_up_our_own_host_name(
    tls: mail.Tls, monkeypatch: pytest.MonkeyPatch
) -> None:
    # smtplib's default EHLO name is socket.getfqdn(): seconds of reverse DNS on some machines.
    seen: dict[str, object] = {}

    class Recorder:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            seen.update(kwargs)

        def ehlo(self) -> None: ...

        def starttls(self, **_kwargs: object) -> None: ...

    monkeypatch.setattr(smtplib, "SMTP", Recorder)
    monkeypatch.setattr(smtplib, "SMTP_SSL", Recorder)
    config = mail.MailConfig(name="support", mail_from="support@mappd.test", smtp_tls=tls)
    mail._smtp_connect(config)
    assert seen["local_hostname"] == "mappd.test"
