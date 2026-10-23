"""The RFQ mailbox: each request email becomes a request keyed by `ids.mail_case`."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

from mappd import db, durable, ids
from mappd.channels.email import Email
from mappd.process import Mailbox

from . import service
from .db import RFQS
from .schemas import MailInfo, RfqIn


@durable.step
async def follows(message_ids: list[str]) -> str | None:
    """The newest request this mail answers: one it names, or one whose quote it names."""
    if not message_ids:
        return None
    ids = sa.literal(message_ids, ARRAY(sa.Text))
    query = (
        sa.select(RFQS.c.external_id)
        .where(
            sa.or_(
                RFQS.c.mail["message_id"].astext == sa.any_(ids),
                RFQS.c.mail["sent_message_id"].astext == sa.any_(ids),
                RFQS.c.external_id == sa.any_(ids),  # keyed by Message-ID before mail_case
            )
        )
        .order_by(RFQS.c.created_at.desc())
        .limit(1)
    )
    async with db.aconnect() as conn:
        row = await (await db.arun(conn, query)).fetchone()
    return str(row["external_id"]) if row else None


async def receive(email: Email) -> None:
    """Accept one request email; the same Message-ID again changes nothing.

    Raises:
        ValueError: the email has no sender address (it cannot be answered).
    """
    if not (email.reply_to or email.sender):
        raise ValueError("the email has no sender address")
    named = [*email.references, *([email.in_reply_to] if email.in_reply_to else [])]
    info = MailInfo(
        message_id=email.message_id,
        references=email.references,
        in_reply_to=email.in_reply_to,
        to=email.to,
        date=email.date,
        attachments=email.attachments,
        follows=await follows([i for i in dict.fromkeys(named) if i != email.message_id]),
    )
    await service.ingest(
        RfqIn(
            external_id=ids.mail_case(email.message_id),
            sender=email.reply_to or email.sender,
            subject=email.subject or "(no subject)",
            body=email.text,
            mail=info,
        )
    )


MAILBOX = Mailbox(name="rfq", receive=receive)
