"""Mailboxes as a durable channel: polled by short workflows, answered through the outbox.

A process declares `Mailbox(name, receive)`. When `MAPPD_<NAME>_MAIL_MODE=imap`, the app
schedules `mappd.mail_poll` for it every `MAPPD_MAIL_POLL_SECONDS`: each tick reads the cursor
(UIDVALIDITY, last UID), fetches the next batch read-only, hands every person's email to
`receive` and only then moves the cursor. A crash in between reads the batch again; `receive`
is idempotent by Message-ID, so nothing is processed twice (AGENTS.md, rules 3 and 4).

`send` is the `mappd.send_email` action: plain SMTP cannot deduplicate by key, so an
interrupted send stops at `unknown` for a person to settle (AGENTS.md, rule 2).
"""

import asyncio
import hashlib
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import psycopg
import sqlalchemy as sa
from dbos import DBOS
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mappd import telemetry
from mappd.actions import PermanentError, action
from mappd.channels.email import (
    Email,
    Fetched,
    ImapClient,
    MailConfig,
    MailError,
    ReplyContext,
    SmtpClient,
    build,
    message_id_for,
    parse,
    parse_headers,
)
from mappd.db import Connection, aconnect, arun, run
from mappd.db.tables import MAIL_CURSORS
from mappd.db.transaction import transaction
from mappd.process import Mailbox, Schedule
from mappd.settings import environment

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mappd.process import Process
    from mappd.settings import Settings

log = logging.getLogger("mappd.mail")

POLL_ENV = "MAPPD_MAIL_POLL_SECONDS"
BATCH = 50
MAILBOXES: dict[str, Mailbox] = {}


class MailOffError(MailError, PermanentError):
    """The mailbox is off: nothing can be sent until its mode is `imap`."""


class CursorRow(BaseModel):
    """Where a mailbox's polling stands."""

    mailbox: str
    uidvalidity: int | None
    last_uid: int
    polled_at: datetime | None


class Polled(BaseModel):
    """One fetch, parsed in the step: the journal keeps text, never attachments' bytes."""

    uidvalidity: int
    uidnext: int
    emails: list[tuple[int, Email]]
    at_end: bool = False  # nothing read: the cursor jumps to the end (new mailbox, no backfill)


def poll_seconds(env: Mapping[str, str] | None = None) -> int:
    """The poll interval, 1 to 59 seconds (default 30).

    Raises:
        ValueError: the setting is not a number in range.
    """
    value = (environment() if env is None else env).get(POLL_ENV, "30").strip() or "30"
    if not value.isdigit() or not 1 <= int(value) <= 59:  # noqa: PLR2004 - a cron seconds step
        raise ValueError(f"{POLL_ENV} must be a whole number of seconds from 1 to 59")
    return int(value)


def config(name: str) -> MailConfig:
    """Mailbox `name`'s settings, read now (they change with a deploy)."""
    return MailConfig.from_env(name)


def imap_client(cfg: MailConfig) -> ImapClient:
    """The IMAP client for a mailbox (tests replace it)."""
    return ImapClient(cfg)


def smtp_client(cfg: MailConfig) -> SmtpClient:
    """The SMTP client for a mailbox (tests replace it)."""
    return SmtpClient(cfg)


def register(processes: Iterable[Process]) -> list[Schedule]:
    """Record every process's mailboxes; one poll schedule per mailbox in `imap` mode.

    Raises:
        ValueError: two mailboxes share a name, or the poll interval is invalid.
        MailConfigError: an `imap` mailbox is missing a setting.
    """
    found: dict[str, Mailbox] = {}
    for process in processes:
        for mailbox in process.mailboxes:
            if mailbox.name in found:
                raise ValueError(f"mailbox {mailbox.name!r} is declared twice")
            found[mailbox.name] = mailbox
    MAILBOXES.clear()
    MAILBOXES.update(found)
    on = [name for name in found if config(name).mode == "imap"]
    if not on:
        return []
    cron = f"*/{poll_seconds()} * * * * *"
    return [
        Schedule(name=f"{name}.mail", workflow=mail_poll, cron=cron, context=name) for name in on
    ]


def _row(row: Any) -> CursorRow:
    return CursorRow(
        mailbox=row["mailbox"],
        uidvalidity=row["uidvalidity"],
        last_uid=row["last_uid"],
        polled_at=row["polled_at"],
    )


def cursor(name: str, settings: Settings | None = None) -> CursorRow | None:
    """Mailbox `name`'s cursor, or None before its first poll (for the CLI and doctor)."""
    from mappd.db import connect  # noqa: PLC0415 - sync connection for callers outside a loop

    with connect(settings) as conn:
        row = run(conn, sa.select(MAIL_CURSORS).where(MAIL_CURSORS.c.mailbox == name)).fetchone()
    return _row(row) if row else None


@DBOS.step(name="mappd.mail_read_cursor")
async def read_cursor(name: str) -> CursorRow | None:
    """The cursor as the tick starts."""
    async with aconnect() as conn:
        query = sa.select(MAIL_CURSORS).where(MAIL_CURSORS.c.mailbox == name)
        row = await (await arun(conn, query)).fetchone()
    return _row(row) if row else None


def _parse(uid: int, size: int, raw: bytes | None, headers: bytes, mail_from: str) -> Email:
    """Never raises: a message the parser cannot read becomes an automated placeholder."""
    try:
        if raw is not None:
            return parse(raw, mail_from=mail_from)
        return parse_headers(headers, size, mail_from=mail_from)
    except Exception:  # a parser bug or hostile mail must not stall the mailbox
        log.exception("mail.unparseable UID %s", uid)
        digest = hashlib.sha256(raw or headers).hexdigest()[:32]
        return Email(message_id=f"<{digest}@mappd.local>", sender="", automated="unparseable")


def _fetch(name: str, cursor: CursorRow | None) -> Polled:
    cfg = config(name)
    at_end = cursor is None and not cfg.backfill
    fetched: Fetched = imap_client(cfg).fetch_since(
        cursor.uidvalidity if cursor else None,
        cursor.last_uid if cursor else 0,
        limit=BATCH,
        start_at_end=at_end,
        reset_to_end=not cfg.backfill,
    )
    emails = [
        (m.uid, _parse(m.uid, m.size, m.raw, m.headers, cfg.mail_from)) for m in fetched.messages
    ]
    reset = cursor is not None and cursor.uidvalidity != fetched.uidvalidity
    return Polled(
        uidvalidity=fetched.uidvalidity,
        uidnext=fetched.uidnext,
        emails=emails,
        at_end=(at_end or (reset and not cfg.backfill)) and not emails,
    )


@DBOS.step(name="mappd.mail_fetch")
async def fetch(name: str, cursor: CursorRow | None) -> Polled:
    """The next batch above the cursor, parsed; read-only on the server.

    Whether to skip the mail already there (no backfill) is decided here, so a replay sees
    the decision the first run made.
    """
    return await asyncio.to_thread(_fetch, name, cursor)


@transaction("mappd.mail_save_cursor")
def save_cursor(conn: Connection, name: str, uidvalidity: int, last_uid: int) -> None:
    """Move the cursor forward; a replay or a late tick never moves it back."""
    insert = pg_insert(MAIL_CURSORS).values(
        mailbox=name, uidvalidity=uidvalidity, last_uid=last_uid, polled_at=sa.func.now()
    )
    run(
        conn,
        insert.on_conflict_do_update(
            index_elements=["mailbox"],
            set_={
                "uidvalidity": insert.excluded.uidvalidity,
                "last_uid": insert.excluded.last_uid,
                "polled_at": sa.func.now(),
                "updated_at": sa.func.now(),
            },
            where=sa.or_(
                MAIL_CURSORS.c.uidvalidity.is_distinct_from(insert.excluded.uidvalidity),
                MAIL_CURSORS.c.last_uid <= insert.excluded.last_uid,
            ),
        ),
    )


async def poll_once(name: str) -> int:
    """One tick for mailbox `name`; returns how many emails `receive` accepted."""
    mailbox = MAILBOXES.get(name)
    if mailbox is None:
        log.warning("mail.poll for unknown mailbox %s: no process declares it", name)
        return 0
    current = await read_cursor(name)
    polled = await fetch(name, current)
    if polled.at_end:
        await save_cursor(name, polled.uidvalidity, polled.uidnext - 1)
        return 0
    accepted = 0
    for _uid, email in polled.emails:
        if email.automated:
            log.info("mail.skipped %s %s: %s", name, email.message_id, email.automated)
            continue
        try:
            await mailbox.receive(email)
        except (ValueError, psycopg.DataError) as e:  # this one cannot become a case
            log.warning("mail.rejected %s %s: %s", name, email.message_id, e)
            continue
        accepted += 1
    if polled.emails:
        last = max(uid for uid, _ in polled.emails)
    elif current is not None and current.uidvalidity == polled.uidvalidity:
        last = current.last_uid
    else:
        last = 0
    await save_cursor(name, polled.uidvalidity, last)
    return accepted


@DBOS.workflow(name="mappd.mail_poll")
async def mail_poll(scheduled_at: datetime, context: Any) -> None:
    """A poll schedule's tick; `context` is the mailbox name."""
    del scheduled_at
    if not await poll_once(str(context)):
        telemetry.idle()


@action("mappd.send_email", on_unknown="escalate")
def send_email(
    idempotency_key: str,
    *,
    mailbox: str,
    to: str,
    subject: str,
    text: str,
    reply_to: dict[str, Any] | None,
    automatic: bool = False,
) -> dict[str, Any]:
    """Send one email over the mailbox's SMTP server; arguments are stored in `actions`.

    The Message-ID comes from the key and the current `MAIL_FROM`, not from the arguments: a
    send that failed (mail off, a refused sender) can be retried after the settings change.
    """
    cfg = config(mailbox)
    if cfg.mode != "imap":
        raise MailOffError(f"mail is off: set MAPPD_{mailbox.upper()}_MAIL_MODE=imap")
    message_id = message_id_for(idempotency_key, cfg.mail_from)
    message = build(
        sender=cfg.mail_from,
        to=to,
        subject=subject,
        text=text,
        message_id=message_id,
        reply_to=ReplyContext.model_validate(reply_to) if reply_to else None,
        automatic=automatic,
    )
    smtp_client(cfg).send(message)
    return {"message_id": message_id}


async def send(
    external_id: str,
    *,
    mailbox: str,
    to: str,
    subject: str,
    text: str,
    reply_to: ReplyContext | None,
    automatic: bool = False,
) -> str:
    """Send a case's one email from `mailbox`; returns its Message-ID.

    `automatic` is for replies no person approved: they carry `Auto-Submitted`.

    The key is `mappd.send_email:<mailbox>:<external_id>`, so a case never sends twice. An
    action a person settled (`resolve --took-effect`) has no stored ID: it is derived again.

    Raises:
        MailOffError: the mailbox is off.
        SmtpRejectedError: the server refused the message.
        OutcomeUnknownError: it may have been sent; settle it with `mappd action resolve`.
    """
    key = f"mappd.send_email:{mailbox}:{external_id}"
    result = await send_email(
        key,
        mailbox=mailbox,
        to=to,
        subject=subject,
        text=text,
        reply_to=reply_to.model_dump(mode="json") if reply_to else None,
        automatic=automatic,
    )
    stored = result.get("message_id")
    return str(stored) if stored else message_id_for(key, config(mailbox).mail_from)
