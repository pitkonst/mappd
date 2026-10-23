"""Mail channel acceptance: real SMTP and IMAP on GreenMail."""

import concurrent.futures as cf
import time
from typing import TYPE_CHECKING, Any

import httpx2
import psycopg
import pytest
import stack

from mappd import ids

if TYPE_CHECKING:
    from collections.abc import Callable

    from mappd.channels.email import Email

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(600)]

TICKETS = f"{stack.APP}/api/v1/support/tickets"
REQUESTS = f"{stack.APP}/api/v1/rfq/requests"


def until[T](probe: Callable[[], T | None], what: str, timeout: float = 60) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            found = probe()
        except httpx2.HTTPError:
            found = None
        if found:
            return found
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what}")


def case(base: str, message_id: str, *statuses: str) -> dict[str, Any]:
    """The case a mail became (keyed `ids.mail_case` of its Message-ID), once in `statuses`."""
    ext = ids.mail_case(message_id)

    def probe() -> dict[str, Any] | None:
        r = httpx2.get(f"{base}/{ext}", timeout=5)
        if r.status_code != 200:
            return None
        data: dict[str, Any] = r.json()
        return data if not statuses or data["status"] in statuses else None

    return until(probe, f"{ext} in {statuses or 'any status'}")


def approve(base: str, message_id: str, version: int, reply: str) -> httpx2.Response:
    return httpx2.post(
        f"{base}/{ids.mail_case(message_id)}/approve",
        json={"version": version, "operator": "olga", "reply": reply},
        timeout=60,
    )


def mail_to(address: str, count: int = 1) -> Callable[[], list[Email] | None]:
    def probe() -> list[Email] | None:
        got = stack.inbox(address)
        return got if len(got) >= count else None

    return probe


def test_customer_mail_becomes_a_ticket_and_the_reply_threads() -> None:
    mid = stack.send_mail(
        "support@mappd.test",
        subject="Sensor offline",
        body="The temperature sensor shows offline since yesterday.",
    )
    t = case(TICKETS, mid, "awaiting_approval")
    assert (t["sender"], t["subject"]) == ("customer@mappd.test", "Sensor offline")
    assert t["mail"]["message_id"] == mid
    reply = "Please power-cycle the controller (kb-101)."
    assert approve(TICKETS, mid, t["version"], reply).json()["result"] == "sent"
    (sent,) = until(mail_to("customer@mappd.test"), "the reply")
    assert sent.in_reply_to == mid
    assert sent.subject == "Re: Sensor offline"
    assert sent.text.startswith(reply)
    assert sent.sender == "support@mappd.test"
    assert case(TICKETS, mid, "sent")["mail"]["sent_message_id"] == sent.message_id


def test_a_customer_answer_becomes_a_ticket_that_follows() -> None:
    first = stack.send_mail(
        "support@mappd.test", subject="Sensor offline", body="Sensor offline in room 4."
    )
    t = case(TICKETS, first, "awaiting_approval")
    approve(TICKETS, first, t["version"], "Please restart it (kb-101).").raise_for_status()
    (ours,) = until(mail_to("customer@mappd.test"), "the reply")
    answer = stack.send_mail(
        "support@mappd.test",
        subject="Re: Sensor offline",
        body="Still offline after the restart.",
        in_reply_to=ours.message_id,
    )
    follow = case(TICKETS, answer)
    assert follow["mail"]["follows"] == ids.mail_case(first)


def test_polling_twice_keeps_one_ticket() -> None:
    mid = stack.send_mail("support@mappd.test", subject="Sensor offline", body="Room 5.")
    case(TICKETS, mid)
    with psycopg.connect(f"{stack.PG}/mappd", autocommit=True) as conn:
        conn.execute("DELETE FROM mappd_mail_cursors WHERE mailbox = 'support'")

    def reread() -> bool:
        with psycopg.connect(f"{stack.PG}/mappd") as conn:
            row = conn.execute(
                "SELECT last_uid FROM mappd_mail_cursors WHERE mailbox = 'support'"
            ).fetchone()
        return row is not None and row[0] >= 1

    until(reread, "a poll that reads the inbox again")
    with psycopg.connect(f"{stack.PG}/mappd") as conn:
        assert conn.execute("SELECT count(*) FROM support_tickets").fetchone() == (1,)


def action(key: str) -> str | None:
    with psycopg.connect(f"{stack.PG}/mappd") as conn:
        row = conn.execute(
            "SELECT status FROM actions WHERE idempotency_key = %s", (key,)
        ).fetchone()
    return str(row[0]) if row else None


def failed_approval() -> str | None:
    with psycopg.connect(f"{stack.PG}/mappd_dbos") as conn:
        row = conn.execute(
            "SELECT workflow_uuid FROM dbos.workflow_status"
            " WHERE name = 'support.approve_and_send' AND status = 'ERROR'"
        ).fetchone()
    return str(row[0]) if row else None


def test_a_crash_inside_the_send_stops_at_unknown() -> None:
    # slow-smtp delivers to slow* recipients at once but answers 5 s later: kill in between.
    mid = stack.send_mail(
        "support@mappd.test",
        sender="slow-customer@mappd.test",
        subject="Sensor offline",
        body="Sensor offline in room 7.",
    )
    t = case(TICKETS, mid, "awaiting_approval")
    with cf.ThreadPoolExecutor(1) as pool:
        pool.submit(approve, TICKETS, mid, t["version"], "Please restart it (kb-101).")
        until(mail_to("slow-customer@mappd.test"), "the reply to be delivered")
        stack.kill()
    stack.start("v1")
    key = f"mappd.send_email:support:{ids.mail_case(mid)}"
    until(lambda: action(key) == "unknown", "the send to be settled as unknown")
    assert case(TICKETS, mid)["status"] == "approved"
    assert len(stack.inbox("slow-customer@mappd.test")) == 1, "never resent by itself"
    workflow_id = until(failed_approval, "the approval to end in ERROR")
    stack.compose("exec", "-T", "app", "mappd", "action", "resolve", key, "--took-effect")
    stack.compose("exec", "-T", "app", "mappd", "workflow", "retry", workflow_id)
    case(TICKETS, mid, "sent")
    assert len(stack.inbox("slow-customer@mappd.test")) == 1, "still one reply"


def test_an_rfq_mail_is_quoted_and_answered_in_thread() -> None:
    mid = stack.send_mail(
        "rfq@mappd.test",
        subject="Request for quotation",
        body="Please quote 12 x Temperature Sensor T100 and 1 x Helio Hub 400.",
    )
    rfq = case(REQUESTS, mid, "awaiting_approval")
    assert rfq["output"]["total"] == "1266.00"
    approve(REQUESTS, mid, rfq["version"], rfq["output"]["draft"]).raise_for_status()
    (sent,) = until(mail_to("customer@mappd.test"), "the quote")
    assert sent.in_reply_to == mid
    assert sent.subject == "Re: Request for quotation"
    assert "1266.00" in sent.text
    assert sent.sender == "rfq@mappd.test"
