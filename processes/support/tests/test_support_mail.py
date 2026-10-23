"""Support on the mail channel: customer mail becomes a ticket, the approved reply threads."""

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa
from dbos import DBOS, WorkflowHandleAsync
from fake_mail import FakeMailbox, FakeSmtp, install, raw
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mappd import ids, mail
from mappd.channels.email import parse
from mappd.db.migrate import sqlalchemy_url, upgrade
from mappd.settings import Settings
from processes.support import domain, process, service, workflow
from processes.support import mail as support_mail
from processes.support.db import METADATA
from processes.support.pages import router as pages
from processes.support.schemas import Decision, SupportOutput

PROCESSES = Path(__file__).parents[2]
ESCALATE = SupportOutput(
    topic="billing",
    route="commercial",
    urgency="normal",
    language="en",
    outcome="escalate",
    escalate_to="billing",
    draft=None,
)


@pytest.fixture
def db(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    engine = sa.create_engine(sqlalchemy_url(dbos_runtime.database_url))
    try:
        METADATA.create_all(engine, checkfirst=True)
    finally:
        engine.dispose()
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM support_tickets")

    async def solve(_t: object) -> SupportOutput:
        return ESCALATE

    monkeypatch.setattr(workflow, "solve", solve)
    return dbos_runtime


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeMailbox, FakeSmtp]:
    box, smtp = install(monkeypatch, "support", backfill=True)
    monkeypatch.setitem(mail.MAILBOXES, "support", support_mail.MAILBOX)
    return box, smtp


def mid() -> str:
    return f"<{uuid4().hex[:12]}@example.com>"


def tick(*message_ids: str) -> None:
    """One poll; then wait for the tickets' workflows in the loop that started them."""

    async def go() -> None:
        await mail.mail_poll(datetime.now(UTC), "support")
        for m in message_ids:
            handle: WorkflowHandleAsync[str] = await DBOS.retrieve_workflow_async(
                domain.ticket_workflow_id(ids.mail_case(m))
            )
            await handle.get_result()

    dbos_async.run(go())


def receive(data: bytes) -> str:
    email = parse(data, mail_from="support@mappd.test")

    async def go() -> None:
        await support_mail.receive(email)
        handle: WorkflowHandleAsync[str] = await DBOS.retrieve_workflow_async(
            domain.ticket_workflow_id(ids.mail_case(email.message_id))
        )
        await handle.get_result()

    dbos_async.run(go())
    return ids.mail_case(email.message_id)


def ticket_row(settings: Settings, external_id: str) -> dict[str, object]:
    with psycopg.connect(settings.database_url) as conn:
        found = conn.execute(
            "SELECT external_id, sender, subject, body, status, mail FROM support_tickets"
            " WHERE external_id = %s OR mail->>'message_id' = %s",
            (external_id, external_id),
        ).fetchone()
    assert found is not None
    keys = ("external_id", "sender", "subject", "body", "status", "mail")
    return dict(zip(keys, found, strict=True))


def test_support_mailbox_is_declared() -> None:
    assert [m.name for m in process.mailboxes] == ["support"]
    assert process.mailboxes[0] is support_mail.MAILBOX


def test_support_mail_becomes_a_ticket(db: Settings, servers: tuple[FakeMailbox, FakeSmtp]) -> None:
    box, _ = servers
    m = mid()
    box.deliver(raw(m, subject="Invoice twice", body="I was billed twice."))
    tick(m)
    row = ticket_row(db, m)
    assert (row["sender"], row["subject"], row["body"], row["status"]) == (
        "anna@example.com",
        "Invoice twice",
        "I was billed twice.",
        "escalated",
    )
    info = row["mail"]
    assert isinstance(info, dict)
    assert info["message_id"] == m
    assert info["to"] == ["support@mappd.test"]
    assert info["follows"] is None


def test_support_same_mail_twice_is_one_ticket(db: Settings) -> None:
    data = raw(mid())
    receive(data)
    receive(data)
    with psycopg.connect(db.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM support_tickets").fetchone() == (1,)


def test_support_mail_is_keyed_for_urls(db: Settings) -> None:
    m = mid()
    key = receive(raw(m))
    assert key == ids.mail_case(m)
    assert ticket_row(db, key)["external_id"] == key


def test_support_mail_stored_under_its_old_key_is_not_taken_twice(db: Settings) -> None:
    m = mid()  # stored as tickets were before mail_case: keyed by the raw Message-ID
    with psycopg.connect(db.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO support_tickets (external_id, sender, subject, body, status, mail)"
            " VALUES (%s, 'anna@example.com', 'Old', 'Old body', 'escalated', %s)",
            (m, json.dumps({"message_id": m})),
        )
    email = parse(raw(m), mail_from="support@mappd.test")

    async def go() -> None:
        await support_mail.receive(email)
        handle: WorkflowHandleAsync[str] = await DBOS.retrieve_workflow_async(
            domain.ticket_workflow_id(m)  # the old key's workflow, not a new key's
        )
        await handle.get_result()

    dbos_async.run(go())
    with psycopg.connect(db.database_url) as conn:
        assert conn.execute("SELECT external_id FROM support_tickets").fetchall() == [(m,)]


def test_0004_makes_the_message_id_unique(fresh_database_url: str) -> None:
    upgrade(Settings(database_url=fresh_database_url, processes_dir=PROCESSES))
    with psycopg.connect(fresh_database_url) as conn:
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'support_tickets_message_id'"
        ).fetchone()
    assert row is not None
    assert "UNIQUE" in row[0]
    assert "message_id" in row[0]


def test_support_mail_without_a_subject_is_accepted(db: Settings) -> None:
    m = receive(raw(mid(), subject=""))
    assert ticket_row(db, m)["subject"] == "(no subject)"


def test_support_mail_attachments_are_recorded(db: Settings) -> None:
    data = (Path(__file__).parents[3] / "tests/channels/eml/attachment.eml").read_bytes()
    m = receive(data)
    info = ticket_row(db, m)["mail"]
    assert isinstance(info, dict)
    assert info["attachments"] == [
        {"filename": "rfq.pdf", "content_type": "application/pdf", "size": 116}
    ]


def sent_ticket(settings: Settings, sent_message_id: str) -> str:
    ext = mid()
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO support_tickets (external_id, sender, subject, body, status, mail)"
            " VALUES (%s, 'anna@example.com', 'Sensor offline', 'Room 3', 'sent', %s)",
            (ext, json.dumps({"message_id": ext, "sent_message_id": sent_message_id})),
        )
    return ext


def test_support_follows_links_a_reply_to_our_answer(db: Settings) -> None:
    ours = mid()
    first = sent_ticket(db, ours)
    m = receive(raw(mid(), subject="Re: Sensor offline", headers=f"In-Reply-To: {ours}"))
    info = ticket_row(db, m)["mail"]
    assert isinstance(info, dict)
    assert info["follows"] == first


def test_support_follows_matches_a_folded_references_header(db: Settings) -> None:
    ours = mid()
    first = sent_ticket(db, ours)
    folded = f"References: <root@example.com>\r\n {ours}"
    m = receive(raw(mid(), headers=folded))
    info = ticket_row(db, m)["mail"]
    assert isinstance(info, dict)
    assert info["follows"] == first


def awaiting_mail(settings: Settings) -> str:
    ext = mid()
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO support_tickets (external_id, sender, subject, body, status, mail)"
            " VALUES (%s, 'anna@example.com', 'Sensor offline', 'It is offline.',"
            " 'awaiting_approval', %s)",
            (ext, json.dumps({"message_id": ext, "references": ["<root@example.com>"]})),
        )
    return ext


def decision() -> Decision:
    return Decision(version=0, operator="olga", reply="Please power-cycle it.")


def test_support_approval_sends_a_threaded_reply(
    db: Settings, servers: tuple[FakeMailbox, FakeSmtp]
) -> None:
    _, smtp = servers
    ext = awaiting_mail(db)
    assert dbos_async.run(service.approve(ext, decision())).result == "sent"
    (sent,) = smtp.sent
    assert sent["In-Reply-To"] == ext
    assert sent["References"] == f"<root@example.com> {ext}"
    assert sent["Subject"] == "Re: Sensor offline"
    assert sent["Auto-Submitted"] is None, "an operator's reply is a person's"
    body = sent.get_content()
    assert body.startswith("Please power-cycle it.")
    assert "> It is offline." in body
    row = ticket_row(db, ext)
    assert row["status"] == "sent"
    info = row["mail"]
    assert isinstance(info, dict)
    assert info["sent_message_id"] == sent["Message-ID"]


def test_support_api_ticket_reply_is_a_fresh_mail(
    db: Settings, servers: tuple[FakeMailbox, FakeSmtp]
) -> None:
    _, smtp = servers
    ext = f"api-{uuid4().hex[:8]}"
    with psycopg.connect(db.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO support_tickets (external_id, sender, subject, body, status)"
            " VALUES (%s, 'anna@example.com', 'Sensor offline', 'Room 3', 'awaiting_approval')",
            (ext,),
        )
    dbos_async.run(service.approve(ext, decision()))
    (sent,) = smtp.sent
    assert sent["In-Reply-To"] is None
    assert sent["Subject"] == "Sensor offline"
    info = ticket_row(db, ext)["mail"]
    assert isinstance(info, dict)
    assert info["sent_message_id"] == sent["Message-ID"]


def test_support_page_shows_attachments_and_follows(db: Settings) -> None:
    ours = mid()
    first = sent_ticket(db, ours)
    data = (Path(__file__).parents[3] / "tests/channels/eml/attachment.eml").read_bytes()
    data = data.replace(b"Message-ID: <att-1@example.com>", f"In-Reply-To: {ours}".encode())
    m = receive(data)
    app = FastAPI(lifespan=dbos_async.lifespan)
    app.include_router(pages)
    with TestClient(app) as client:
        page = client.get(f"/support/tickets/{m}")
    assert page.status_code == 200
    assert "rfq.pdf" in page.text
    assert "application/pdf" in page.text
    assert "follows" in page.text
    assert first in page.text.replace("&lt;", "<").replace("&gt;", ">")


def test_0003_adds_the_mail_column(fresh_database_url: str) -> None:
    upgrade(Settings(database_url=fresh_database_url, processes_dir=PROCESSES))
    with psycopg.connect(fresh_database_url) as conn:
        row = conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns"
            " WHERE table_name = 'support_tickets' AND column_name = 'mail'"
        ).fetchone()
    assert row == ("jsonb", "YES")


def test_support_answers_the_reply_to_address(db: Settings) -> None:
    m = receive(raw(mid(), sender="noreply@forms.example", headers="Reply-To: anna@example.com"))
    assert ticket_row(db, m)["sender"] == "anna@example.com"


# No "%": Starlette's TestClient decodes the path twice (uvicorn decodes it once).
ODD = "<owner/repo/pull/1?x=1#c@github.example>"


def test_support_message_ids_with_slashes_work_in_api_and_pages(
    db: Settings, servers: tuple[FakeMailbox, FakeSmtp]
) -> None:
    with psycopg.connect(db.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO support_tickets (external_id, sender, subject, body, status, mail)"
            " VALUES (%s, 'anna@example.com', 'PR', 'b', 'awaiting_approval', %s)",
            (ODD, json.dumps({"message_id": ODD})),
        )
    app = FastAPI(lifespan=dbos_async.lifespan)
    app.include_router(pages)
    app.include_router(process.router)  # type: ignore[arg-type]
    path = quote(ODD, safe="")
    with TestClient(app) as client:
        assert client.get(f"/api/v1/support/tickets/{path}").json()["external_id"] == ODD
        page = client.get(f"/support/tickets/{path}")
        assert page.status_code == 200
        assert f'hx-post="/support/tickets/{quote(ODD, safe="/")}/approve"' in page.text
        listed = client.get("/support/rows?status=awaiting_approval").text
        approved = client.post(
            f"/api/v1/support/tickets/{path}/approve",
            json={"version": 0, "operator": "olga", "reply": "Merged."},
        )
    assert approved.json()["result"] == "sent", approved.text
    assert ticket_row(db, ODD)["status"] == "sent"
    assert f'href="/support/tickets/{quote(ODD, safe="/")}"' in listed
