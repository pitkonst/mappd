"""The service layer both the API and the pages call."""

import asyncio
from email.message import EmailMessage
from uuid import uuid4

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID, WorkflowStatus
from fake_mail import install
from psycopg.types.json import Jsonb

from mappd import events, ids, mail
from mappd.autonomy import Policy
from mappd.db.migrate import sqlalchemy_url
from mappd.events import Event
from mappd.settings import Settings
from processes.support import domain, service, trust, workflow
from processes.support.db import METADATA
from processes.support.schemas import Decision, Escalation, Resolution


@pytest.fixture
def db(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Support tables in the session DB, emptied; a mailer that records sends."""
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
    return dbos_runtime


@pytest.fixture
def mails(monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    return install(monkeypatch, "support")[1].sent


def awaiting(settings: Settings, status: str = "awaiting_approval") -> str:
    external_id = f"svc-{uuid4().hex[:10]}"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO support_tickets (external_id, sender, subject, body, status)"
            " VALUES (%s, 'anna@example.com', 'Sensor offline', 'Room 3', %s)",
            (external_id, status),
        )
    return external_id


def decision(version: int = 0, reply: str = "Please power-cycle it (kb-101).") -> Decision:
    return Decision(version=version, operator="olga", reply=reply)


def test_list_pages_newest_first_and_filters(db: Settings) -> None:
    made = [awaiting(db) for _ in range(3)]
    awaiting(db, status="escalated")
    first = dbos_async.run(service.list_tickets(status="awaiting_approval", limit=2))
    assert [t.external_id for t in first.items] == made[::-1][:2]
    assert first.next is not None
    rest = dbos_async.run(
        service.list_tickets(status="awaiting_approval", limit=2, before=first.next)
    )
    assert [t.external_id for t in rest.items] == [made[0]]
    assert rest.next is None


def test_status_all_lists_every_status_as_the_queue_tab_does(db: Settings) -> None:
    mine = {awaiting(db), awaiting(db, status="escalated")}
    listed = dbos_async.run(service.list_tickets(status="all")).items
    assert mine <= {t.external_id for t in listed}


def test_bad_cursor_is_refused(db: Settings) -> None:
    with pytest.raises(service.BadCursorError):
        dbos_async.run(service.list_tickets(before="not-a-cursor"))


def test_get_unknown_ticket(db: Settings) -> None:
    with pytest.raises(service.NotFoundError):
        dbos_async.run(service.get_ticket("nope"))


def test_approve_sends_once_and_a_repeat_says_so(db: Settings, mails: list[EmailMessage]) -> None:
    ext = awaiting(db)
    first = dbos_async.run(service.approve(ext, decision()))
    again = dbos_async.run(service.approve(ext, decision()))
    assert (first.result, first.repeated) == ("sent", False)
    assert (again.result, again.repeated) == ("sent", True)
    assert len(mails) == 1


def test_a_key_makes_a_repeat_with_the_same_body(db: Settings, mails: list[EmailMessage]) -> None:
    ext = awaiting(db)
    first = dbos_async.run(service.approve(ext, decision(), idempotency_key="k-1"))
    again = dbos_async.run(service.approve(ext, decision(), idempotency_key="k-1"))
    assert (first.repeated, again.repeated) == (False, True)
    assert len(mails) == 1


def test_same_key_different_reply_is_422(db: Settings, mails: list[EmailMessage]) -> None:
    ext = awaiting(db)
    dbos_async.run(service.approve(ext, decision(), idempotency_key="k-2"))
    with pytest.raises(service.IdempotencyMismatchError):
        dbos_async.run(service.approve(ext, decision(reply="Other."), idempotency_key="k-2"))


def test_stale_version_reports_the_current_one(db: Settings, mails: list[EmailMessage]) -> None:
    ext = awaiting(db)
    dbos_async.run(service.approve(ext, decision(), idempotency_key="a"))
    with pytest.raises(service.StaleVersionError) as exc:
        dbos_async.run(service.approve(ext, decision(), idempotency_key="b"))
    assert (exc.value.current_version, exc.value.status) == (2, "sent")


def test_approve_unknown_ticket_starts_nothing(db: Settings) -> None:
    with pytest.raises(service.NotFoundError):
        dbos_async.run(service.approve("nope", decision()))


def test_articles_keep_citation_order_and_skip_unknown(db: Settings) -> None:
    zeros = "[" + ",".join(["0"] * 64) + "]"
    with psycopg.connect(db.database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM support_kb")
        for kb, title in (("kb-1", "One"), ("kb-2", "Two")):
            conn.execute(
                "INSERT INTO support_kb (id, title, body, embedding) VALUES (%s, %s, '', %s)",
                (kb, title, zeros),
            )
    got = dbos_async.run(service.articles(["kb-2", "kb-9", "kb-1"]))
    assert [(a.id, a.title) for a in got] == [("kb-2", "Two"), ("kb-1", "One")]
    assert dbos_async.run(service.articles([])) == []


def test_a_racing_decision_with_another_reply_is_refused(
    db: Settings, mails: list[EmailMessage], monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = awaiting(db)
    dbos_async.run(service.approve(ext, decision(reply="A."), idempotency_key="race"))
    real = DBOS.get_workflow_status_async
    seen: list[str] = []

    async def racing(workflow_id: str) -> WorkflowStatus | None:
        seen.append(workflow_id)  # the first look misses the other operator's start
        return None if len(seen) == 1 else await real(workflow_id)

    monkeypatch.setattr(DBOS, "get_workflow_status_async", racing)
    with pytest.raises(service.IdempotencyMismatchError):
        dbos_async.run(service.approve(ext, decision(reply="B."), idempotency_key="race"))
    assert len(mails) == 1


def test_a_failed_send_names_the_workflow_to_retry(
    db: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = awaiting(db)

    async def mailer_down(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("mailer down")

    monkeypatch.setattr(mail, "send", mailer_down)
    expected = ids.from_key("support.approve", f"{ext}:f-1")
    for _ in range(2):  # a repeat reports the same failure, not a bare error
        with pytest.raises(service.SendFailedError) as exc:
            dbos_async.run(service.approve(ext, decision(), idempotency_key="f-1"))
        assert exc.value.workflow_id == expected


def test_shadowed_approval_sends_nothing(
    db: Settings, mails: list[EmailMessage], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow, "current_policy", lambda: Policy(shadow=True))
    ext = awaiting(db)
    result = dbos_async.run(service.approve(ext, decision(), idempotency_key="sh-1"))
    assert result.result == "shadowed"
    assert mails == []


def test_approval_records_edits_and_topic_correction(
    db: Settings, mails: list[EmailMessage]
) -> None:
    ext = awaiting(db)  # no output: draft unknown, so any reply counts as unedited? see below
    d = Decision(version=0, operator="olga", reply="Changed.", topic="billing")
    dbos_async.run(service.approve(ext, d, idempotency_key="ed-1"))
    got = dbos_async.run(service.get_ticket(ext))
    assert got.decision is not None
    assert got.decision.operator == "olga"
    assert got.decision.corrected.topic == "billing"


def escalated(settings: Settings) -> str:
    return awaiting(settings, status="escalated")


def test_escalate_moves_an_awaiting_ticket_without_sending(
    db: Settings, mails: list[EmailMessage]
) -> None:
    ext = awaiting(db)
    e = Escalation(version=0, operator="olga", escalate_to="billing")
    assert dbos_async.run(service.escalate(ext, e)).result == "escalated"
    t = dbos_async.run(service.get_ticket(ext))
    assert (t.status, t.version) == ("escalated", 1)
    assert t.decision is not None
    assert t.decision.corrected.escalate_to == "billing"
    assert mails == []


def test_resolve_records_a_false_escalation(db: Settings) -> None:
    ext = escalated(db)
    r = Resolution(version=0, operator="olga", agent_could_handle=True, outcome="reply")
    assert dbos_async.run(service.resolve(ext, r)).result == "resolved"
    t = dbos_async.run(service.get_ticket(ext))
    assert t.status == "resolved"
    assert t.decision is not None
    assert t.decision.agent_could_handle is True


def test_resolve_twice_is_a_conflict(db: Settings) -> None:
    ext = escalated(db)
    dbos_async.run(
        service.resolve(ext, Resolution(version=0, operator="olga"), idempotency_key="a")
    )
    with pytest.raises(service.StaleVersionError) as exc:
        dbos_async.run(
            service.resolve(ext, Resolution(version=0, operator="olga"), idempotency_key="b")
        )
    assert exc.value.status == "resolved"


def test_resolve_an_awaiting_ticket_is_a_conflict(db: Settings) -> None:
    ext = awaiting(db)
    with pytest.raises(service.StaleVersionError):
        dbos_async.run(service.resolve(ext, Resolution(version=0, operator="olga")))


def test_escalate_repeat_is_flagged(db: Settings) -> None:
    ext = awaiting(db)
    e = Escalation(version=0, operator="olga", escalate_to="sales")
    dbos_async.run(service.escalate(ext, e, idempotency_key="k"))
    assert dbos_async.run(service.escalate(ext, e, idempotency_key="k")).repeated is True


OUTPUT_BASE = {
    "topic": "how_to",
    "route": "technical",
    "urgency": "normal",
    "language": "en",
    "outcome": "reply",
    "escalate_to": None,
    "draft": {"reply": "Do X.", "citations": ["kb-101"]},
}


def decide_row(
    settings: Settings, ext: str, status: str, record: dict[str, object], topic: str
) -> None:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE support_tickets SET status=%s, decision=%s, output=%s WHERE external_id=%s",
            (status, Jsonb(record), Jsonb({**OUTPUT_BASE, "topic": topic}), ext),
        )


def test_list_filters_by_category_and_decision(db: Settings) -> None:
    a, b, c = awaiting(db), awaiting(db), awaiting(db)
    decide_row(
        db,
        a,
        "sent",
        {"kind": "send", "operator": "auto", "corrected": {}, "decided_at": "2026-10-16T00:00:00Z"},
        "how_to",
    )
    decide_row(
        db,
        b,
        "sent",
        {
            "kind": "send",
            "operator": "olga",
            "edited_reply": True,
            "corrected": {},
            "decided_at": "2026-10-16T00:00:00Z",
        },
        "how_to",
    )
    decide_row(
        db,
        c,
        "resolved",
        {
            "kind": "resolve",
            "operator": "olga",
            "agent_could_handle": True,
            "corrected": {},
            "decided_at": "2026-10-16T00:00:00Z",
        },
        "billing",
    )

    def ids_of(category: str | None = None, decision: str | None = None) -> set[str]:
        page = dbos_async.run(service.list_tickets(category=category, decision=decision))
        return {t.external_id for t in page.items}

    assert ids_of(category="how_to") == {a, b}
    assert ids_of(decision="auto") == {a}
    assert ids_of(decision="edited") == {b}
    assert ids_of(decision="false_escalation") == {c}


def test_resolving_an_operator_escalation_keeps_it(db: Settings) -> None:
    ext = awaiting(db)
    dbos_async.run(
        service.escalate(ext, Escalation(version=0, operator="olga", escalate_to="billing"))
    )
    dbos_async.run(service.resolve(ext, Resolution(version=1, operator="ivan")))
    t = dbos_async.run(service.get_ticket(ext))
    assert t.decision is not None
    assert t.decision.kind == "resolve"
    assert t.decision.prior is not None
    assert (t.decision.prior.kind, t.decision.prior.corrected.escalate_to) == (
        "escalate",
        "billing",
    )
    report = dbos_async.run(trust.report(30, Policy()))
    assert (report.total.proposals, report.total.without_edits) == (1, 0), "still an edit"
    assert report.total.resolutions == 0, "not an agent escalation"


def test_an_operator_escalation_event_names_the_team(db: Settings) -> None:
    ext = awaiting(db)

    async def scenario() -> Event:
        stream = events.BROKER.subscribe("support")
        first = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        e = Escalation(version=0, operator="olga", escalate_to="billing")
        await service.escalate(ext, e)
        return await asyncio.wait_for(first, 5)

    event = dbos_async.run(scenario())
    assert event.type == "escalated"
    assert (event.data["escalate_to"], event.data["outcome"]) == ("billing", "escalate")


def test_a_hard_rule_escalation_cannot_be_judged_unnecessary(db: Settings) -> None:
    ext = awaiting(db, status="escalated")
    refund = {**OUTPUT_BASE, "topic": "refund", "outcome": "escalate", "escalate_to": "sales"}
    with psycopg.connect(db.database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE support_tickets SET output = %s WHERE external_id = %s", (Jsonb(refund), ext)
        )
    claim = Resolution(version=0, operator="olga", agent_could_handle=True)
    with pytest.raises(service.HardRuleError, match="refund always goes to sales"):
        dbos_async.run(service.resolve(ext, claim))
    replied = Resolution(version=0, operator="olga", outcome="reply")
    with pytest.raises(service.HardRuleError, match="escalating was right"):
        dbos_async.run(service.resolve(ext, replied))
    fine = Resolution(version=0, operator="olga")
    assert dbos_async.run(service.resolve(ext, fine)).result == "resolved"


@DBOS.workflow(name="test.support.service_stops")
def processing_stops() -> None:
    raise RuntimeError("the classifier timed out")


def test_a_new_ticket_names_its_stopped_workflow(db: Settings) -> None:
    ext = awaiting(db, status="new")
    wid = domain.ticket_workflow_id(ext)
    with SetWorkflowID(wid), pytest.raises(RuntimeError):
        DBOS.start_workflow(processing_stops).get_result()
    found = dbos_async.run(service.get_ticket(ext))
    assert found.processing is not None
    assert (found.processing.workflow_id, found.processing.remedy) == (wid, "retry")
    fine = awaiting(db)
    assert dbos_async.run(service.get_ticket(fine)).processing is None, "only a new case"
