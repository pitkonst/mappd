"""process_rfq end to end with a fixed decision: autonomy, events, the journal."""

import asyncio
from email.message import EmailMessage
from uuid import uuid4

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID
from fake_mail import install

from mappd import events, workflows
from mappd.autonomy import Policy
from mappd.db.migrate import sqlalchemy_url
from mappd.events import Event
from mappd.settings import Settings
from processes.rfq import domain, workflow
from processes.rfq.db import METADATA
from processes.rfq.events import WEBHOOKS
from processes.rfq.schemas import Extracted, Extraction, RfqOutput


@pytest.fixture
def env(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    for target in WEBHOOKS:
        monkeypatch.delenv(target.url_env, raising=False)
    engine = sa.create_engine(sqlalchemy_url(dbos_runtime.database_url))
    try:
        METADATA.create_all(engine, checkfirst=True)
    finally:
        engine.dispose()
    return install(monkeypatch, "rfq")[1].sent


def decided(*, custom: bool = False, qty: int = 12) -> RfqOutput:
    return domain.solve_output(
        Extraction(
            lines=[Extracted(requested="sensors", quantity=qty, sku="SEN-T100", confidence="high")],
            needed_by_days=None,
            custom_request=custom,
            language="en",
        )
    )


def new_rfq(settings: Settings) -> str:
    ext = f"fl-{uuid4().hex[:10]}"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO rfq_requests (external_id, sender, subject, body)"
            " VALUES (%s, 'anna@example.com', 'Quote', '12 sensors')",
            (ext,),
        )
    return ext


def run(
    ext: str, output: RfqOutput, policy: Policy, monkeypatch: pytest.MonkeyPatch
) -> list[Event]:
    async def solve(_rfq: object) -> RfqOutput:
        return output

    monkeypatch.setattr(workflow, "solve", solve)
    monkeypatch.setattr(workflow, "current_policy", lambda: policy)

    async def go() -> list[Event]:
        got: list[Event] = []

        async def collect() -> None:
            async for e in events.BROKER.subscribe("rfq"):
                got.append(e)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        with SetWorkflowID(domain.rfq_workflow_id(ext)):
            handle = await DBOS.start_workflow_async(workflow.process_rfq, ext)
        await handle.get_result()
        child = domain.approval_workflow_id(ext, 1)
        if await DBOS.get_workflow_status_async(child) is not None:
            await (await DBOS.retrieve_workflow_async(child)).get_result()
        await asyncio.sleep(0.1)
        task.cancel()
        return got

    return dbos_async.run(go())


def status(settings: Settings, ext: str) -> str:
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            "SELECT status FROM rfq_requests WHERE external_id = %s", (ext,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_a_review_quote_waits_and_emits_approval_needed(
    dbos_runtime: Settings, env: list[EmailMessage], monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = new_rfq(dbos_runtime)
    got = run(ext, decided(), Policy(), monkeypatch)
    assert [(e.type, e.version) for e in got] == [("created", 0), ("approval_needed", 1)]
    assert got[1].data["total"] == "576.00"
    assert status(dbos_runtime, ext) == "awaiting_approval"
    assert env == []


def test_an_auto_quote_is_sent_without_approval(
    dbos_runtime: Settings, env: list[EmailMessage], monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = new_rfq(dbos_runtime)
    run(ext, decided(), Policy(categories={"quote": "auto"}), monkeypatch)
    assert status(dbos_runtime, ext) == "sent"
    (sent,) = env
    assert sent["Auto-Submitted"] == "auto-replied", "auto-responders do not answer it"


def test_escalations_never_auto_send(
    dbos_runtime: Settings, env: list[EmailMessage], monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = new_rfq(dbos_runtime)
    run(ext, decided(custom=True), Policy(default="auto"), monkeypatch)
    assert status(dbos_runtime, ext) == "escalated"
    assert env == []


def test_the_policy_is_a_journaled_step(
    dbos_runtime: Settings, env: list[EmailMessage], monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = new_rfq(dbos_runtime)
    run(ext, decided(), Policy(), monkeypatch)
    with workflows.client(dbos_runtime) as c:
        steps = c.list_workflow_steps(domain.rfq_workflow_id(ext), load_output=True)
    [level] = [s for s in steps if s["function_name"] == "rfq.autonomy_level"]
    assert level["output"] == "review"
