"""Autonomy and shadow mode in the support workflows."""

from email.message import EmailMessage
from uuid import uuid4

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID
from fake_mail import install

from mappd import workflows
from mappd.autonomy import Policy
from mappd.db.migrate import sqlalchemy_url
from mappd.settings import Settings
from processes.support import domain, workflow
from processes.support.db import METADATA
from processes.support.schemas import Category, Draft, Outcome, SupportOutput


@pytest.fixture
def env(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    engine = sa.create_engine(sqlalchemy_url(dbos_runtime.database_url))
    try:
        METADATA.create_all(engine, checkfirst=True)
    finally:
        engine.dispose()
    return install(monkeypatch, "support")[1].sent


def with_policy(monkeypatch: pytest.MonkeyPatch, policy: Policy) -> None:
    monkeypatch.setattr(workflow, "current_policy", lambda: policy)


def decided(topic: Category, outcome: Outcome = "reply") -> SupportOutput:
    escalate = outcome == "escalate"
    return SupportOutput(
        topic=topic,
        route="technical",
        urgency="normal",
        language="en",
        outcome=outcome,
        escalate_to="service" if escalate else None,
        draft=None if escalate else Draft(reply="Do X.", citations=["kb-101"]),
    )


def new_ticket(settings: Settings, status: str = "new") -> str:
    ext = f"au-{uuid4().hex[:10]}"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO support_tickets (external_id, sender, subject, body, status)"
            " VALUES (%s, 'anna@example.com', 'How do I', 'Reset?', %s)",
            (ext, status),
        )
    return ext


def row(settings: Settings, ext: str) -> tuple[str, dict[str, object] | None]:
    with psycopg.connect(settings.database_url) as conn:
        found = conn.execute(
            "SELECT status, decision FROM support_tickets WHERE external_id = %s", (ext,)
        ).fetchone()
    assert found is not None
    return found[0], found[1]


def process(ext: str, output: SupportOutput, monkeypatch: pytest.MonkeyPatch) -> str:
    async def solve(_t: object) -> SupportOutput:
        return output

    monkeypatch.setattr(workflow, "solve", solve)

    async def go() -> str:
        with SetWorkflowID(domain.ticket_workflow_id(ext)):
            handle = await DBOS.start_workflow_async(workflow.process_ticket, ext)
        status = await handle.get_result()
        child = await DBOS.get_workflow_status_async(domain.approval_workflow_id(ext, 1))
        if child is not None:  # settle the auto send in this loop (see dbos_async)
            await (
                await DBOS.retrieve_workflow_async(domain.approval_workflow_id(ext, 1))
            ).get_result()
        return str(status)

    return dbos_async.run(go())


def test_an_auto_category_is_sent_without_approval(
    dbos_runtime: Settings, env: list[EmailMessage], monkeypatch: pytest.MonkeyPatch
) -> None:
    with_policy(monkeypatch, Policy(categories={"how_to": "auto"}))
    ext = new_ticket(dbos_runtime)
    process(ext, decided("how_to"), monkeypatch)
    status, decision = row(dbos_runtime, ext)
    assert status == "sent"
    assert decision is not None
    assert (decision["operator"], decision["kind"], decision["edited_reply"]) == (
        "auto",
        "send",
        False,
    )
    (sent,) = env
    assert sent["Auto-Submitted"] == "auto-replied", "auto-responders do not answer it"


def test_a_review_category_waits(
    dbos_runtime: Settings, env: list[EmailMessage], monkeypatch: pytest.MonkeyPatch
) -> None:
    with_policy(monkeypatch, Policy(categories={"how_to": "auto"}))
    ext = new_ticket(dbos_runtime)
    process(ext, decided("billing"), monkeypatch)
    assert row(dbos_runtime, ext) == ("awaiting_approval", None)
    assert env == []


def test_escalations_never_auto_send(
    dbos_runtime: Settings, env: list[EmailMessage], monkeypatch: pytest.MonkeyPatch
) -> None:
    with_policy(monkeypatch, Policy(default="auto"))
    ext = new_ticket(dbos_runtime)
    process(ext, decided("refund", outcome="escalate"), monkeypatch)
    assert row(dbos_runtime, ext)[0] == "escalated"
    assert env == []


def test_shadow_mode_records_the_auto_decision_and_sends_nothing(
    dbos_runtime: Settings, env: list[EmailMessage], monkeypatch: pytest.MonkeyPatch
) -> None:
    with_policy(monkeypatch, Policy(shadow=True, categories={"how_to": "auto"}))
    ext = new_ticket(dbos_runtime)
    process(ext, decided("how_to"), monkeypatch)
    status, decision = row(dbos_runtime, ext)
    assert status == "shadowed"
    assert decision is not None
    assert decision["shadow"] is True
    assert env == []


def test_policy_is_journaled_for_replays(
    dbos_runtime: Settings, env: list[EmailMessage], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The level is a recorded step: a replay or a fork reads it from the journal."""
    with_policy(monkeypatch, Policy())  # review
    ext = new_ticket(dbos_runtime)
    process(ext, decided("how_to"), monkeypatch)
    with workflows.client(dbos_runtime) as c:
        steps = c.list_workflow_steps(domain.ticket_workflow_id(ext), load_output=True)
    [level] = [s for s in steps if s["function_name"] == "support.autonomy_level"]
    assert level["output"] == "review"


def test_approval_keeps_the_3b_journal_prefix(
    dbos_runtime: Settings, env: list[EmailMessage], monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_ticket, claim_approval come first, as in 3b: a pre-3c approval can be retried."""
    with_policy(monkeypatch, Policy(shadow=True))
    ext = new_ticket(dbos_runtime, status="awaiting_approval")
    wid = domain.approval_workflow_id(ext, 0)

    async def go() -> str:
        with SetWorkflowID(wid):
            handle = await DBOS.start_workflow_async(
                workflow.approve_and_send, ext, 0, "olga", "Reply."
            )
        return str(await handle.get_result())

    assert dbos_async.run(go()) == "shadowed"
    with workflows.client(dbos_runtime) as c:
        names = [s["function_name"] for s in c.list_workflow_steps(wid, load_output=False)]
    assert names[:2] == ["support.load_ticket", "support.claim_approval"]
    status, decision = row(dbos_runtime, ext)
    assert status == "shadowed"
    assert decision is not None
    assert decision["shadow"] is True
