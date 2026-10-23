"""mappd mail: the processes' mailboxes — where polling stands, and test mail into one."""

from datetime import datetime
from email.utils import make_msgid
from typing import Annotated

import typer

from mappd import mail
from mappd.actions import OutcomeUnknownError
from mappd.app import load
from mappd.channels.email import MailConfig, MailConfigError, MailError, build
from mappd.cli._common import (
    DB_ERRORS,
    EXIT_USAGE,
    JsonFlag,
    emit,
    fail,
    load_settings,
    table,
)
from mappd.web.templates import utc

app = typer.Typer(help="Mailboxes of the processes.", no_args_is_help=True)


def _mailboxes() -> list[str]:
    settings = load_settings()
    return [m.name for p in load(settings).values() for m in p.mailboxes]


def _polled_at(value: object) -> str:
    return utc(value, "%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else "-"


@app.command()
def status(as_json: JsonFlag = False) -> None:
    """Each mailbox of the registered processes: its mode and how far polling has read.

    Example:
        mappd mail status --json
    """
    rows = []
    for name in _mailboxes():
        try:
            mode: str = MailConfig.from_env(name).mode
        except MailConfigError as e:
            mode = f"invalid: {e}"
        try:
            cursor = mail.cursor(name)
        except DB_ERRORS as e:
            fail(f"app database unavailable: {e}")
        rows.append(
            {
                "name": name,
                "mode": mode,
                "uidvalidity": cursor.uidvalidity if cursor else None,
                "last_uid": cursor.last_uid if cursor else None,
                "polled_at": cursor.polled_at if cursor else None,
            }
        )
    emit(
        {"mailboxes": rows},
        as_json,
        table(
            ["MAILBOX", "MODE", "LAST_UID", "POLLED_AT"],
            [
                [
                    str(r["name"]),
                    str(r["mode"]),
                    str(r["last_uid"]) if r["last_uid"] is not None else "-",
                    _polled_at(r["polled_at"]),
                ]
                for r in rows
            ],
        )
        or ["no mailboxes"],
    )


@app.command()
def inject(
    mailbox: Annotated[str, typer.Argument(help="The mailbox to send to, e.g. support.")],
    sender: Annotated[str, typer.Option("--from", help="The From address.")],
    subject: Annotated[str, typer.Option(help="The subject line.")],
    body: Annotated[str, typer.Option(help="The plain-text body.")] = "",
    in_reply_to: Annotated[str | None, typer.Option(help="A Message-ID this mail answers.")] = None,
    as_json: JsonFlag = False,
) -> None:
    """Send one message to a mailbox's address over its SMTP settings (dev and demos).

    Exit 2 for an unknown mailbox or one that is off, 1 when the server refuses.

    Example:
        mappd mail inject support --from customer@mappd.test --subject "Sensor offline"
    """
    if mailbox not in _mailboxes():
        fail(f"no mailbox {mailbox!r} in the registered processes", EXIT_USAGE)
    try:
        cfg = MailConfig.from_env(mailbox)
    except MailConfigError as e:
        fail(str(e), EXIT_USAGE)
    if cfg.mode != "imap":
        fail(f"mailbox {mailbox} is off: set MAPPD_{mailbox.upper()}_MAIL_MODE=imap", EXIT_USAGE)
    domain = sender.rpartition("@")[2] or "mappd.local"
    message = build(
        sender=sender,
        to=cfg.mail_from,
        subject=subject,
        text=body,
        message_id=make_msgid(domain=domain),
        reply_to=None,
    )
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    try:
        mail.smtp_client(cfg).send(message)
    except (MailError, OutcomeUnknownError) as e:
        fail(str(e))
    emit(
        {"message_id": message["Message-ID"], "to": cfg.mail_from},
        as_json,
        [f"sent {message['Message-ID']} to {cfg.mail_from}"],
    )
