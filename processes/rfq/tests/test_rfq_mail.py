"""RFQ on the mail channel: a request email becomes a request, the approved quote threads."""

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
from psycopg.types.json import Jsonb

from mappd import ids, mail
from mappd.autonomy import Policy
from mappd.channels.email import parse
from mappd.db.migrate import sqlalchemy_url, upgrade
from mappd.settings import Settings
from processes.rfq import domain, pages, process, service, workflow
from processes.rfq import mail as rfq_mail
from processes.rfq.db import METADATA
from processes.rfq.schemas import Decision, Extracted, Extraction, RfqIn, RfqOutput

PROCESSES = Path(__file__).parents[2]
EML = Path(__file__).parents[3] / "tests/channels/eml"
QUOTE = domain.solve_output(
    Extraction(
        lines=[Extracted(requested="sensors", quantity=12, sku="SEN-T100", confidence="high")],
        needed_by_days=None,
        custom_request=False,
        language="en",
    )
)


@pytest.fixture
def seen(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> list[RfqIn]:
    """RFQ tables emptied; `solve` returns QUOTE and records what the agents were given."""
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    engine = sa.create_engine(sqlalchemy_url(dbos_runtime.database_url))
    try:
        METADATA.create_all(engine, checkfirst=True)
    finally:
        engine.dispose()
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM rfq_requests")
    given: list[RfqIn] = []

    async def solve(rfq: RfqIn) -> RfqOutput:
        given.append(rfq)
        return QUOTE

    monkeypatch.setattr(workflow, "solve", solve)
    monkeypatch.setattr(workflow, "current_policy", Policy)
    return given


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeMailbox, FakeSmtp]:
    box, smtp = install(monkeypatch, "rfq", backfill=True)
    monkeypatch.setitem(mail.MAILBOXES, "rfq", rfq_mail.MAILBOX)
    return box, smtp


def mid() -> str:
    return f"<{uuid4().hex[:12]}@example.com>"


async def settle(message_id: str) -> None:
    handle: WorkflowHandleAsync[str] = await DBOS.retrieve_workflow_async(
        domain.rfq_workflow_id(ids.mail_case(message_id))
    )
    await handle.get_result()


def receive(data: bytes) -> str:
    email = parse(data, mail_from="rfq@mappd.test")

    async def go() -> None:
        await rfq_mail.receive(email)
        await settle(email.message_id)

    dbos_async.run(go())
    return ids.mail_case(email.message_id)


def row(settings: Settings, external_id: str) -> dict[str, object]:
    with psycopg.connect(settings.database_url) as conn:
        found = conn.execute(
            "SELECT sender, subject, body, status, mail FROM rfq_requests"
            " WHERE external_id = %s OR mail->>'message_id' = %s",
            (external_id, external_id),
        ).fetchone()
    assert found is not None
    return dict(zip(("sender", "subject", "body", "status", "mail"), found, strict=True))


def test_rfq_mailbox_is_declared() -> None:
    assert list(process.mailboxes) == [rfq_mail.MAILBOX]
    assert rfq_mail.MAILBOX.name == "rfq"


def test_rfq_mail_becomes_a_request(
    seen: list[RfqIn], dbos_runtime: Settings, servers: tuple[FakeMailbox, FakeSmtp]
) -> None:
    box, _ = servers
    m = mid()
    box.deliver(raw(m, to="rfq@mappd.test", subject="RFQ", body="Please quote 12 x SEN-T100."))

    async def go() -> None:
        await mail.mail_poll(datetime.now(UTC), "rfq")
        await settle(m)

    dbos_async.run(go())
    got = row(dbos_runtime, m)
    assert (got["sender"], got["subject"], got["body"]) == (
        "anna@example.com",
        "RFQ",
        "Please quote 12 x SEN-T100.",
    )
    info = got["mail"]
    assert isinstance(info, dict)
    assert info["message_id"] == m


def test_rfq_same_mail_twice_is_one_request(seen: list[RfqIn], dbos_runtime: Settings) -> None:
    data = raw(mid(), to="rfq@mappd.test")
    receive(data)
    receive(data)
    with psycopg.connect(dbos_runtime.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM rfq_requests").fetchone() == (1,)


def test_rfq_mail_attachments_are_recorded(seen: list[RfqIn], dbos_runtime: Settings) -> None:
    m = receive((EML / "attachment.eml").read_bytes())
    info = row(dbos_runtime, m)["mail"]
    assert isinstance(info, dict)
    assert [a["filename"] for a in info["attachments"]] == ["rfq.pdf"]
    (given,) = seen
    assert given.body == "Please quote the items in the attached list."


def test_rfq_follows_links_a_reply_to_our_quote(seen: list[RfqIn], dbos_runtime: Settings) -> None:
    ours, first = mid(), mid()
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO rfq_requests (external_id, sender, subject, body, status, mail)"
            " VALUES (%s, 'anna@example.com', 'RFQ', 'b', 'sent', %s)",
            (first, json.dumps({"message_id": first, "sent_message_id": ours})),
        )
    m = receive(raw(mid(), to="rfq@mappd.test", headers=f"In-Reply-To: {ours}"))
    info = row(dbos_runtime, m)["mail"]
    assert isinstance(info, dict)
    assert info["follows"] == first


def test_rfq_approval_sends_a_threaded_quote(
    seen: list[RfqIn], dbos_runtime: Settings, servers: tuple[FakeMailbox, FakeSmtp]
) -> None:
    _, smtp = servers
    ext = mid()
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO rfq_requests (external_id, sender, subject, body, status, output, mail)"
            " VALUES (%s, 'anna@example.com', 'Request for quotation', '12 sensors please',"
            " 'awaiting_approval', %s, %s)",
            (ext, Jsonb(QUOTE.model_dump(mode="json")), json.dumps({"message_id": ext})),
        )
    reply = QUOTE.draft or "Quote"
    result = dbos_async.run(service.approve(ext, Decision(version=0, operator="olga", reply=reply)))
    assert result.result == "sent"
    (sent,) = smtp.sent
    assert sent["Subject"] == "Re: Request for quotation"
    assert sent["In-Reply-To"] == ext
    assert sent.get_content().startswith(reply.rstrip())
    assert "> 12 sensors please" in sent.get_content()
    info = row(dbos_runtime, ext)["mail"]
    assert isinstance(info, dict)
    assert info["sent_message_id"] == sent["Message-ID"]


def test_rfq_0002_adds_the_mail_column(fresh_database_url: str) -> None:
    upgrade(Settings(database_url=fresh_database_url, processes_dir=PROCESSES))
    with psycopg.connect(fresh_database_url) as conn:
        found = conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns"
            " WHERE table_name = 'rfq_requests' AND column_name = 'mail'"
        ).fetchone()
    assert found == ("jsonb", "YES")


def test_rfq_answers_the_reply_to_address(seen: list[RfqIn], dbos_runtime: Settings) -> None:
    m = receive(raw(mid(), sender="noreply@forms.example", headers="Reply-To: anna@example.com"))
    assert row(dbos_runtime, m)["sender"] == "anna@example.com"


def test_rfq_message_ids_with_slashes_work_in_api_and_pages(
    seen: list[RfqIn], dbos_runtime: Settings
) -> None:
    odd = "<orders/2026/17?a=1#x@shop.example>"  # no "%": TestClient decodes paths twice
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO rfq_requests (external_id, sender, subject, body, status, output)"
            " VALUES (%s, 'anna@example.com', 'RFQ', 'b', 'awaiting_approval', %s)",
            (odd, Jsonb(QUOTE.model_dump(mode="json"))),
        )
    app = FastAPI(lifespan=dbos_async.lifespan)
    app.state.processes = ["rfq"]
    app.include_router(pages.router)
    app.include_router(process.router)  # type: ignore[arg-type]
    with TestClient(app) as client:
        api = client.get(f"/api/v1/rfq/requests/{quote(odd, safe='')}")
        page = client.get(f"/rfq/requests/{quote(odd, safe='')}")
    assert api.json()["external_id"] == odd
    assert page.status_code == 200
    assert f'hx-post="/rfq/requests/{quote(odd, safe="/")}/approve"' in page.text
