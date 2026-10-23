"""Support's workflows emit events; the API streams them."""

import asyncio
from uuid import uuid4

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID
from fake_mail import install

from mappd import events
from mappd.db.migrate import sqlalchemy_url
from mappd.events import Event
from mappd.settings import Settings
from processes.support import domain, workflow
from processes.support.db import TICKETS
from processes.support.events import WEBHOOKS
from processes.support.schemas import Draft, SupportOutput


@pytest.fixture
def ticket(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    for target in WEBHOOKS:  # SSE only: no deliveries in this test
        monkeypatch.delenv(target.url_env, raising=False)
    engine = sa.create_engine(sqlalchemy_url(dbos_runtime.database_url))
    try:
        TICKETS.create(engine, checkfirst=True)
    finally:
        engine.dispose()
    ext = f"ev-{uuid4().hex[:10]}"
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO support_tickets (external_id, sender, subject, body, status)"
            " VALUES (%s, 'anna@example.com', 'Sensor offline', 'Room 3', 'awaiting_approval')",
            (ext,),
        )
    install(monkeypatch, "support")
    return ext


def test_approval_emits_closed_with_the_final_version(ticket: str) -> None:
    async def scenario() -> Event:
        stream = events.BROKER.subscribe("support")
        first = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        with SetWorkflowID(domain.approval_workflow_id(ticket, 0)):
            handle = await DBOS.start_workflow_async(
                workflow.approve_and_send, ticket, 0, "olga", "Reply."
            )
        assert await handle.get_result() == "sent"
        return await asyncio.wait_for(first, 5)

    e = dbos_async.run(scenario())
    assert (e.type, e.case_id, e.version) == ("closed", ticket, 2)
    assert e.data["status"] == "sent"
    assert e.id == f"support:{ticket}:closed:2"


def test_a_conflict_emits_nothing(ticket: str) -> None:
    async def scenario() -> list[Event]:
        got: list[Event] = []

        async def collect() -> None:
            async for e in events.BROKER.subscribe("support"):
                got.append(e)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        with SetWorkflowID(domain.approval_workflow_id(ticket, 7)):
            handle = await DBOS.start_workflow_async(
                workflow.approve_and_send, ticket, 7, "olga", "Reply."
            )
        assert await handle.get_result() == "conflict"
        await asyncio.sleep(0.1)
        task.cancel()
        return got

    assert dbos_async.run(scenario()) == []


def decided(topic: str) -> SupportOutput:
    escalate = topic == "refund"
    return SupportOutput(
        topic=topic,
        route="technical",
        urgency="normal",
        language="en",
        outcome="escalate" if escalate else "reply",
        escalate_to="sales" if escalate else None,
        draft=None if escalate else Draft(reply="Power-cycle it.", citations=["kb-101"]),
    )


def processed(ticket: str, topic: str, monkeypatch: pytest.MonkeyPatch) -> list[Event]:
    """Run process_ticket with a fixed decision; the support events it emitted.

    `solve` is a plain function (agent calls are the journaled steps), so patching it keeps
    model instances out of the workflow, where DBOS cannot serialize them.
    """

    async def solve(_ticket: object) -> SupportOutput:
        return decided(topic)

    monkeypatch.setattr(workflow, "solve", solve)

    async def scenario() -> list[Event]:
        got: list[Event] = []

        async def collect() -> None:
            async for e in events.BROKER.subscribe("support"):
                got.append(e)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        with SetWorkflowID(domain.ticket_workflow_id(ticket)):
            handle = await DBOS.start_workflow_async(workflow.process_ticket, ticket)
        await handle.get_result()
        await asyncio.sleep(0.1)
        task.cancel()
        return got

    return dbos_async.run(scenario())


def new_ticket(settings: Settings) -> str:
    ext = f"ev-{uuid4().hex[:10]}"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO support_tickets (external_id, sender, subject, body)"
            " VALUES (%s, 'anna@example.com', 'Sensor offline', 'Room 3')",
            (ext,),
        )
    return ext


def test_a_reply_ticket_emits_created_then_approval_needed(
    dbos_runtime: Settings, ticket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = new_ticket(dbos_runtime)
    got = processed(ext, "malfunction", monkeypatch)
    assert [(e.type, e.version) for e in got] == [("created", 0), ("approval_needed", 1)]
    assert got[1].data["outcome"] == "reply"


def test_a_refund_emits_escalated(
    dbos_runtime: Settings, ticket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = new_ticket(dbos_runtime)
    got = processed(ext, "refund", monkeypatch)
    assert [e.type for e in got] == ["created", "escalated"]
    assert got[1].data["escalate_to"] == "sales"
