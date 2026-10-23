"""Email as a channel: parse what arrives, build what goes out.

The Message-ID is the case key: a process stores each email under it, and a re-read (a poll
after a crash, a reset UIDVALIDITY) changes nothing. A message without one gets a hash of its
bytes, so the key stays stable. Automated mail (auto-replies, bounces, our own sends) is
recognized here, in code, so a mail loop cannot start (AGENTS.md, rule 6).

Python's standard library only; durable code lives in `mappd.mail`.
"""

import contextlib
import hashlib
import imaplib
import io
import re
import smtplib
import ssl
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.generator import BytesGenerator
from email.message import EmailMessage, Message
from email.utils import format_datetime, getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, SecretStr, model_validator

from mappd.actions import OutcomeUnknownError, PermanentError
from mappd.settings import environment

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

OVERSIZE_TEXT = "[message over 25 MB not downloaded]"
QUOTE_LINES = 50
MAX_ID = 250  # longer Message-IDs are hashed: they key unique indexes
DAEMONS = frozenset({"mailer-daemon", "postmaster"})
PRECEDENCE = frozenset({"bulk", "junk", "list", "auto_reply"})
_ID = re.compile(r"<[^<>\s]+>")
_RE = re.compile(r"^(\s*re\s*:\s*)+", re.IGNORECASE)


class Attachment(BaseModel):
    """An attachment as recorded: its content is not kept."""

    filename: str | None
    content_type: str
    size: int


class Email(BaseModel):
    """The parts of an email a process uses."""

    message_id: str
    sender: str
    sender_name: str = ""
    reply_to: str | None = None  # where answers go when not to `sender` (Reply-To)
    to: list[str] = []
    subject: str = ""
    text: str = ""
    in_reply_to: str | None = None
    references: list[str] = []
    date: datetime | None = None
    attachments: list[Attachment] = []
    automated: str | None = None  # why it is automated mail; None for a person's message
    truncated: bool = False  # over the size limit: headers only

    @model_validator(mode="before")
    @classmethod
    def _no_nul(cls, data: Any) -> Any:
        """Postgres text cannot hold NUL: a NUL in customer mail must not stall a mailbox."""
        if isinstance(data, dict):
            return {
                k: v.replace("\x00", "")
                if isinstance(v, str)
                else [i.replace("\x00", "") if isinstance(i, str) else i for i in v]
                if isinstance(v, list)
                else v
                for k, v in data.items()
            }
        return data


class ReplyContext(BaseModel):
    """What a reply needs from the message it answers."""

    message_id: str
    references: list[str] = []
    subject: str = ""
    sender: str = ""
    date: datetime | None = None
    text: str = ""

    @classmethod
    def of(cls, email: Email) -> ReplyContext:
        """The reply context of a parsed email."""
        return cls(
            message_id=email.message_id,
            references=email.references,
            subject=email.subject,
            sender=email.sender,
            date=email.date,
            text=email.text,
        )


def normalize_id(value: str) -> str | None:
    """A Message-ID as `<local@domain>`, whatever brackets and spaces it came with."""
    inner = value.strip().strip("<>").strip()
    return f"<{inner}>" if inner else None


def id_list(value: str | None) -> list[str]:
    """The Message-IDs of a References or In-Reply-To header, in order."""
    if not value:
        return []
    found = _ID.findall(value)
    if found:
        return [normalize_id(v) or v for v in found]
    return [i for v in value.split() if (i := normalize_id(v))]


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _header(message: Message, name: str) -> str:
    try:
        value = message.get(name)
    except ValueError, IndexError, TypeError:  # a malformed header the parser cannot model
        return ""
    return _one_line(str(value)) if value is not None else ""


def _address(value: str) -> tuple[str, str]:
    name, addr = parseaddr(value)
    return _one_line(name), addr.strip()


def automated(message: Message, mail_from: str | None) -> str | None:
    """Why this message is automated mail (never a case), or None for a person's message."""
    auto = _header(message, "Auto-Submitted").split(";")[0].strip().lower()
    sender = _address(_header(message, "From"))[1].lower()
    own = _address(mail_from)[1].lower() if mail_from else ""
    rules = (
        ("auto-submitted", bool(auto) and auto != "no"),
        ("precedence", _header(message, "Precedence").lower() in PRECEDENCE),
        (
            "autoreply-header",
            message.get("X-Autoreply") is not None or message.get("X-Autorespond") is not None,
        ),
        ("daemon", sender.partition("@")[0] in DAEMONS),
        ("report", message.get_content_type() == "multipart/report"),
        ("own", bool(own) and sender == own),
    )
    return next((reason for reason, hit in rules if hit), None)


class _Text(HTMLParser):
    SKIP = frozenset({"script", "style", "head", "title"})
    BLOCK = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Readable text of an HTML body: block tags become line breaks, scripts and styles go."""
    parser = _Text()
    parser.feed(html)
    parser.close()
    lines = [
        re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(parser.parts).split("\n")
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True)
    data = payload if isinstance(payload, bytes) else b""
    try:
        text = data.decode(part.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n")


def _leaves(message: Message) -> Iterator[Message]:
    """Body parts in order, without descending into attached messages."""
    if message.is_multipart() and not message.get_content_type().startswith("message/"):
        for part in cast("list[Message]", message.get_payload()):
            yield from _leaves(part)
    else:
        yield message


def _is_attachment(part: Message) -> bool:
    return (
        part.get_content_disposition() == "attachment"
        or part.get_filename() is not None
        or part.get_content_type().startswith("message/")
    )


def _size(part: Message) -> int:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return len(payload)
    try:
        return len(part.as_bytes())
    except Exception:
        return len(str(part.get_payload()))


def _envelope(message: Message, raw: bytes, mail_from: str | None) -> Email:
    sender_name, sender = _address(_header(message, "From"))
    to = [addr for _, addr in getaddresses([_header(message, "To")]) if addr]
    date = None
    if stamp := _header(message, "Date"):
        try:
            date = parsedate_to_datetime(stamp)
        except TypeError, ValueError, IndexError:
            date = None
        if date is not None:
            date = date.replace(tzinfo=UTC) if date.tzinfo is None else date.astimezone(UTC)
    in_reply_to = id_list(_header(message, "In-Reply-To"))
    message_id = normalize_id(_header(message, "Message-ID"))
    if message_id is None or len(message_id) > MAX_ID:
        seed = message_id.encode() if message_id else raw
        message_id = f"<{hashlib.sha256(seed).hexdigest()[:32]}@mappd.local>"
    return Email(
        message_id=message_id,
        sender=sender,
        sender_name=sender_name,
        reply_to=_address(_header(message, "Reply-To"))[1] or None,
        to=to,
        subject=_header(message, "Subject"),
        in_reply_to=in_reply_to[0] if in_reply_to else None,
        references=id_list(_header(message, "References")),
        date=date,
        automated=automated(message, mail_from),
    )


def _message(raw: bytes) -> Message:
    return message_from_bytes(raw, policy=policy.default)


def parse(raw: bytes, *, mail_from: str | None = None) -> Email:
    """An email from its raw bytes; never raises on malformed mail.

    The text is the first `text/plain` part, else the first `text/html` part as text.
    Attachments are listed by name, type and size. `mail_from` is the mailbox's own address:
    a message from it is automated (`"own"`).
    """
    message = _message(raw)
    email = _envelope(message, raw, mail_from)
    plain: str | None = None
    html: str | None = None
    for part in _leaves(message):
        if _is_attachment(part):
            email.attachments.append(
                Attachment(
                    filename=part.get_filename(),
                    content_type=part.get_content_type(),
                    size=_size(part),
                )
            )
        elif part.get_content_type() == "text/plain" and plain is None:
            plain = _decode(part)
        elif part.get_content_type() == "text/html" and html is None:
            html = _decode(part)
    if plain is not None:
        email.text = plain.strip().replace("\x00", "")
    elif html is not None:
        email.text = html_to_text(html).replace("\x00", "")
    return email


def parse_headers(raw_headers: bytes, size: int, *, mail_from: str | None = None) -> Email:
    """An email over the size limit, from its headers only."""
    del size  # the caller decided it is too large; the text says so
    email = _envelope(_message(raw_headers), raw_headers, mail_from)
    email.text = OVERSIZE_TEXT
    email.truncated = True
    return email


def reply_subject(subject: str) -> str:
    """`Re: <subject>`, with any `Re:` prefixes already there collapsed to one."""
    rest = _RE.sub("", _one_line(subject))
    return f"Re: {rest}" if rest else "Re:"


def quote(ctx: ReplyContext, limit: int = QUOTE_LINES) -> str:
    """The original under a reply: an attribution line, then at most `limit` quoted lines."""
    who = ctx.sender or "you"
    head = f"On {format_datetime(ctx.date)}, {who} wrote:" if ctx.date else f"{who} wrote:"
    lines = ctx.text.strip().splitlines()[:limit]
    return "\n".join([head, *(f"> {line}" if line else ">" for line in lines)])


def message_id_for(key: str, sender: str) -> str:
    """The Message-ID for an action key: the same key sends the same ID."""
    domain = _address(sender)[1].rpartition("@")[2] or "mappd.local"
    return f"<{hashlib.sha256(key.encode()).hexdigest()[:32]}@{domain}>"


def build(
    *,
    sender: str,
    to: str,
    subject: str,
    text: str,
    message_id: str,
    reply_to: ReplyContext | None,
    automatic: bool = False,
) -> EmailMessage:
    """A UTF-8 plain-text email; with `reply_to` it threads under that message and quotes it.

    `automatic` marks a reply no person approved (`Auto-Submitted: auto-replied`, RFC 3834),
    so auto-responders on the other side stay quiet and no mail loop starts.
    """
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = message_id
    if automatic:
        message["Auto-Submitted"] = "auto-replied"
    body = text.rstrip() + "\n"
    if reply_to is None:
        message["Subject"] = _one_line(subject)
    else:
        message["Subject"] = reply_subject(subject)
        message["In-Reply-To"] = reply_to.message_id
        chain = dict.fromkeys([*reply_to.references, reply_to.message_id])
        message["References"] = " ".join(chain)
        body += "\n" + quote(reply_to) + "\n"
    message.set_content(body, charset="utf-8")
    return message


# --- Configuration and clients ------------------------------------------------------------

type Tls = Literal["ssl", "starttls", "none"]
TIMEOUT = 30.0
MAX_BYTES = 26_214_400
IMAP_PORTS: dict[str, int] = {"ssl": 993, "starttls": 143, "none": 143}
SMTP_PORTS: dict[str, int] = {"ssl": 465, "starttls": 587, "none": 25}
TRUE = frozenset({"1", "true", "yes", "on"})


class MailError(RuntimeError):
    """A mailbox or mail server problem; the text never carries a password."""


class MailConfigError(MailError):
    """A mailbox's settings are missing or invalid; the text names the variable."""


class SmtpTemporaryError(MailError):
    """The server did not take the message this time (refused connection, 4xx): retry."""


class SmtpRejectedError(MailError, PermanentError):
    """The server refused the message for good (5xx): retrying cannot help."""


class MailConfig(BaseModel):
    """One mailbox's IMAP and SMTP settings, from `MAPPD_<NAME>_…`."""

    name: str
    mode: Literal["off", "imap"] = "off"
    mail_from: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: SecretStr = SecretStr("")
    imap_folder: str = "INBOX"
    imap_tls: Tls = "ssl"
    backfill: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_tls: Tls = "starttls"
    max_bytes: int = MAX_BYTES

    @classmethod
    def from_env(cls, name: str, env: Mapping[str, str] | None = None) -> MailConfig:
        """The settings of mailbox `name`; mode `off` needs nothing else.

        Raises:
            MailConfigError: a required variable is missing, or a value is invalid.

        `env` defaults to the process environment over `.env`, as for every MAPP-D setting.
        """
        values = environment() if env is None else env
        prefix = f"MAPPD_{name.upper()}_"

        def get(key: str, default: str = "") -> str:
            return values.get(prefix + key, default).strip()

        def choice(key: str, default: str, allowed: tuple[str, ...]) -> str:
            value = get(key, default) or default
            if value not in allowed:
                raise MailConfigError(f"{prefix}{key} must be one of {', '.join(allowed)}")
            return value

        def port(key: str, default: int) -> int:
            value = get(key)
            if not value:
                return default
            if not value.isdigit():
                raise MailConfigError(f"{prefix}{key} must be a port number")
            return int(value)

        mode = choice("MAIL_MODE", "off", ("off", "imap"))
        if mode == "off":
            return cls(name=name)
        for key in ("MAIL_FROM", "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "SMTP_HOST"):
            if not get(key):
                raise MailConfigError(f"{prefix}{key} is not set (mail mode is imap)")
        imap_tls = choice("IMAP_TLS", "ssl", ("ssl", "starttls", "none"))
        smtp_tls = choice("SMTP_TLS", "starttls", ("ssl", "starttls", "none"))
        return cls(
            name=name,
            mode="imap",
            mail_from=get("MAIL_FROM"),
            imap_host=get("IMAP_HOST"),
            imap_port=port("IMAP_PORT", IMAP_PORTS[imap_tls]),
            imap_user=get("IMAP_USER"),
            imap_password=SecretStr(get("IMAP_PASSWORD")),
            imap_folder=get("IMAP_FOLDER") or "INBOX",
            imap_tls=cast("Tls", imap_tls),
            backfill=get("IMAP_BACKFILL").lower() in TRUE,
            smtp_host=get("SMTP_HOST"),
            smtp_port=port("SMTP_PORT", SMTP_PORTS[smtp_tls]),
            smtp_user=get("SMTP_USER"),
            smtp_password=SecretStr(get("SMTP_PASSWORD")),
            smtp_tls=cast("Tls", smtp_tls),
        )

    def redact(self, text: str) -> str:
        """`text` with this mailbox's passwords removed."""
        for secret in (self.imap_password, self.smtp_password):
            if value := secret.get_secret_value():
                text = text.replace(value, "***")
        return text


class FetchedMessage(BaseModel):
    """One message above the cursor: its bytes, or only its headers when over the limit."""

    uid: int
    size: int
    headers: bytes
    raw: bytes | None


class Fetched(BaseModel):
    """What one fetch saw: the folder's UIDVALIDITY and UIDNEXT, and the new messages."""

    uidvalidity: int
    uidnext: int
    messages: list[FetchedMessage]


def _context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _imap_connect(config: MailConfig) -> imaplib.IMAP4:
    if config.imap_tls == "ssl":
        return imaplib.IMAP4_SSL(
            config.imap_host, config.imap_port, ssl_context=_context(), timeout=TIMEOUT
        )
    conn = imaplib.IMAP4(config.imap_host, config.imap_port, timeout=TIMEOUT)
    if config.imap_tls == "starttls":
        conn.starttls(ssl_context=_context())
    return conn


_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")


def _payload(data: list[Any]) -> tuple[bytes, bytes]:
    """The (metadata, literal) pair of a single-message FETCH response."""
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2:  # noqa: PLR2004 - (meta, literal)
            return bytes(item[0]), bytes(item[1])
    raise MailError("IMAP FETCH returned no message data")


class ImapClient:
    """Reads one mailbox folder without changing it: EXAMINE and BODY.PEEK only."""

    def __init__(
        self,
        config: MailConfig,
        *,
        factory: Callable[[MailConfig], imaplib.IMAP4] | None = None,
    ) -> None:
        self.config = config
        self.factory = factory or _imap_connect

    def _fail(self, error: BaseException) -> MailError:
        where = f"IMAP {self.config.imap_host}:{self.config.imap_port}"
        return MailError(self.config.redact(f"{where}: {type(error).__name__}: {error}"))

    def _open(self) -> imaplib.IMAP4:
        conn = self.factory(self.config)
        try:
            conn.login(self.config.imap_user, self.config.imap_password.get_secret_value())
        except BaseException:
            self._close(conn)
            raise
        return conn

    @staticmethod
    def _close(conn: imaplib.IMAP4) -> None:
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            conn.logout()

    def _examine(self, conn: imaplib.IMAP4) -> int:
        typ, data = conn.select(self.config.imap_folder, readonly=True)
        if typ != "OK":
            raise MailError(f"IMAP folder {self.config.imap_folder!r} cannot be opened")
        return int(data[0] or 0)

    def check(self) -> str:
        """Log in and examine the folder: `"<folder>, <n> messages"`.

        Raises:
            MailError: the server, the login or the folder failed.
        """
        try:
            conn = self._open()
            try:
                count = self._examine(conn)
            finally:
                self._close(conn)
        except (imaplib.IMAP4.error, OSError) as e:
            raise self._fail(e) from None
        return f"{self.config.imap_folder}, {count} messages"

    def fetch_since(
        self,
        uidvalidity: int | None,
        last_uid: int,
        *,
        limit: int = 50,
        start_at_end: bool = False,
        reset_to_end: bool = False,
    ) -> Fetched:
        """Messages with a UID above `last_uid`, lowest first, at most `limit`.

        A UIDVALIDITY other than `uidvalidity` restarts from UID 0, or — with `reset_to_end` —
        from the end, like `start_at_end`, which reads nothing and reports UIDNEXT so a new
        mailbox begins after the mail already there.

        Raises:
            MailError: the server, the login or the folder failed.
        """
        try:
            conn = self._open()
            try:
                return self._fetch(
                    conn,
                    uidvalidity=uidvalidity,
                    last_uid=last_uid,
                    limit=limit,
                    start_at_end=start_at_end,
                    reset_to_end=reset_to_end,
                )
            finally:
                self._close(conn)
        except (imaplib.IMAP4.error, OSError) as e:
            raise self._fail(e) from None

    def _fetch(
        self,
        conn: imaplib.IMAP4,
        *,
        uidvalidity: int | None,
        last_uid: int,
        limit: int,
        start_at_end: bool,
        reset_to_end: bool,
    ) -> Fetched:
        self._examine(conn)
        current = int(conn.response("UIDVALIDITY")[1][0] or 0)
        if uidvalidity is not None and current != uidvalidity:
            last_uid = 0
            start_at_end = start_at_end or reset_to_end
        _, found = conn.uid("SEARCH", f"UID {last_uid + 1}:*")
        uids = sorted(int(u) for u in (found[0] or b"").split() if int(u) > last_uid)
        nxt = conn.response("UIDNEXT")[1][0]
        uidnext = int(nxt) if nxt else max(uids, default=last_uid) + 1
        if start_at_end:
            return Fetched(uidvalidity=current, uidnext=uidnext, messages=[])
        messages = []
        for uid in uids[:limit]:
            _, data = conn.uid("FETCH", str(uid), "(RFC822.SIZE BODY.PEEK[HEADER])")
            meta, headers = _payload(data)
            size = int(m.group(1)) if (m := _SIZE.search(meta)) else len(headers)
            raw = None
            if size <= self.config.max_bytes:
                _, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = _payload(data)[1]
            messages.append(FetchedMessage(uid=uid, size=size, headers=headers, raw=raw))
        return Fetched(uidvalidity=current, uidnext=uidnext, messages=messages)


def helo_name(config: MailConfig) -> str:
    """The name we greet the SMTP server with: the From address's domain, else localhost.

    Given explicitly because smtplib's default is `socket.getfqdn()`, a reverse DNS lookup that
    takes seconds on some machines (5 s on a stock macOS) and runs on every connection.
    """
    domain = parseaddr(config.mail_from)[1].rpartition("@")[2]
    return domain or "localhost"


def _smtp_connect(config: MailConfig) -> smtplib.SMTP:
    name = helo_name(config)
    if config.smtp_tls == "ssl":
        return smtplib.SMTP_SSL(
            config.smtp_host,
            config.smtp_port,
            local_hostname=name,
            timeout=TIMEOUT,
            context=_context(),
        )
    conn = smtplib.SMTP(config.smtp_host, config.smtp_port, local_hostname=name, timeout=TIMEOUT)
    if config.smtp_tls == "starttls":
        conn.ehlo()
        conn.starttls(context=_context())
        conn.ehlo()
    return conn


PERMANENT = 500


class SmtpClient:
    """Sends one message per connection and says how a failure should be treated.

    Before the message data is sent, a failure is `SmtpTemporaryError` (4xx, connection problems)
    or `SmtpRejectedError` (5xx). Once the data is on its way, a timeout or a dropped connection
    may still have delivered it: `OutcomeUnknownError`, for a person to settle.
    """

    def __init__(
        self,
        config: MailConfig,
        *,
        factory: Callable[[MailConfig], smtplib.SMTP] | None = None,
    ) -> None:
        self.config = config
        self.factory = factory or _smtp_connect

    def _where(self) -> str:
        return f"SMTP {self.config.smtp_host}:{self.config.smtp_port}"

    def _by_code(self, code: int, reply: bytes | str) -> MailError:
        text = reply.decode(errors="replace") if isinstance(reply, bytes) else reply
        detail = self.config.redact(f"{self._where()}: {code} {_one_line(text)}")
        return SmtpRejectedError(detail) if code >= PERMANENT else SmtpTemporaryError(detail)

    def _open(self) -> smtplib.SMTP:
        conn: smtplib.SMTP | None = None
        try:
            conn = self.factory(self.config)
            if self.config.smtp_user:
                conn.login(self.config.smtp_user, self.config.smtp_password.get_secret_value())
        except smtplib.SMTPResponseException as e:
            if conn is not None:
                self._quit(conn)
            raise self._by_code(e.smtp_code, e.smtp_error) from None
        except (smtplib.SMTPException, OSError) as e:
            if conn is not None:
                self._quit(conn)
            detail = f"{self._where()}: {type(e).__name__}: {e}"
            raise SmtpTemporaryError(self.config.redact(detail)) from None
        return conn

    def check(self) -> str:
        """Connect, greet and log in: `"<host>:<port> ok"`.

        Raises:
            MailError: the server or the login failed.
        """
        conn = self._open()
        try:
            conn.ehlo_or_helo_if_needed()
        except (smtplib.SMTPException, OSError) as e:
            raise SmtpTemporaryError(self.config.redact(f"{self._where()}: {e}")) from None
        finally:
            self._quit(conn)
        return f"{self.config.smtp_host}:{self.config.smtp_port} ok"

    @staticmethod
    def _quit(conn: smtplib.SMTP) -> None:
        with contextlib.suppress(smtplib.SMTPException, OSError):
            conn.quit()

    def send(self, message: EmailMessage) -> None:
        """Send `message` to its `To` addresses.

        Raises:
            SmtpTemporaryError: not sent; worth retrying.
            SmtpRejectedError: not sent; the server refused it for good.
            OutcomeUnknownError: the data was sent but the reply was lost.
        """
        sender = _address(str(message["From"]))[1]
        recipients = [addr for _, addr in getaddresses([str(message["To"])]) if addr]
        buffer = io.BytesIO()
        BytesGenerator(buffer, policy=message.policy.clone(linesep="\r\n")).flatten(message)
        conn = self._open()
        try:
            try:
                conn.ehlo_or_helo_if_needed()
                code, reply = conn.mail(sender)
                if code != 250:  # noqa: PLR2004 - SMTP OK
                    raise self._by_code(code, reply)
                for recipient in recipients:
                    code, reply = conn.rcpt(recipient)
                    if code not in (250, 251):
                        raise self._by_code(code, reply)
            except smtplib.SMTPResponseException as e:
                raise self._by_code(e.smtp_code, e.smtp_error) from None
            except (smtplib.SMTPException, OSError) as e:
                detail = f"{self._where()}: {type(e).__name__}: {e}"
                raise SmtpTemporaryError(self.config.redact(detail)) from None
            try:
                conn.data(buffer.getvalue())
            except smtplib.SMTPResponseException as e:  # the server answered: not delivered
                raise self._by_code(e.smtp_code, e.smtp_error) from None
            except (smtplib.SMTPException, OSError) as e:
                detail = f"{self._where()}: {type(e).__name__} after sending the data"
                raise OutcomeUnknownError(self.config.redact(detail)) from None
        finally:
            self._quit(conn)
