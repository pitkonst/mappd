"""RFQ's service layer: approval with re-pricing, escalation, resolution, repeats, shadow."""

from email.message import EmailMessage
from uuid import uuid4

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa
from fake_mail import install
from psycopg.types.json import Jsonb

from mappd.autonomy import Policy
from mappd.db.migrate import sqlalchemy_url
from mappd.settings import Settings
from processes.rfq import domain, service, workflow
from processes.rfq.db import METADATA
from processes.rfq.schemas import Decision, Escalation, Extracted, Extraction, LineFix, Resolution

QUOTE = domain.solve_output(
    Extraction(
        lines=[
            Extracted(
                requested="temperature sensors", quantity=12, sku="SEN-T100", confidence="high"
            )
        ],
        needed_by_days=None,
        custom_request=False,
        language="en",
    )
)


def edited(note: str) -> str:
    """An operator's reply: their words around the quote's figures, which stay as drafted."""
    return f"{note}\n\n{QUOTE.draft}"


@pytest.fixture
def db(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    engine = sa.create_engine(sqlalchemy_url(dbos_runtime.database_url))
    try:
        METADATA.create_all(engine, checkfirst=True)
    finally:
        engine.dispose()
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM rfq_requests")
    return dbos_runtime


@pytest.fixture
def mails(monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    return install(monkeypatch, "rfq")[1].sent


def awaiting(settings: Settings, status: str = "awaiting_approval") -> str:
    ext = f"rfq-{uuid4().hex[:10]}"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO rfq_requests (external_id, sender, subject, body, status, output)"
            " VALUES (%s, 'anna@example.com', 'Quote', '12 sensors', %s, %s)",
            (ext, status, Jsonb(QUOTE.model_dump(mode="json"))),
        )
    return ext


def body_of(message: EmailMessage) -> str:
    return str(message.get_content())


def test_approve_sends_once_and_a_repeat_says_so(db: Settings, mails: list[EmailMessage]) -> None:
    ext = awaiting(db)
    d = Decision(version=1, operator="olga", reply=QUOTE.draft or "")
    with psycopg.connect(db.database_url, autocommit=True) as conn:
        conn.execute("UPDATE rfq_requests SET version = 1 WHERE external_id = %s", (ext,))
    first = dbos_async.run(service.approve(ext, d, idempotency_key="k"))
    again = dbos_async.run(service.approve(ext, d, idempotency_key="k"))
    assert (first.result, first.repeated, again.repeated) == ("sent", False, True)
    assert len(mails) == 1
    assert "576.00" in body_of(mails[0])
    t = dbos_async.run(service.get_rfq(ext))
    assert t.decision is not None
    assert (t.decision.edited_reply, t.decision.corrected.lines) == (False, None)


def test_line_fix_reprices_the_quote(db: Settings, mails: list[EmailMessage]) -> None:
    ext = awaiting(db)
    d = Decision(
        version=0,
        operator="olga",
        reply=QUOTE.draft or "",
        lines=[LineFix(sku="SEN-T100", quantity=2)],
    )
    dbos_async.run(service.approve(ext, d))
    body = body_of(mails[0])
    assert "96.00" in body
    assert "576.00" not in body
    t = dbos_async.run(service.get_rfq(ext))
    assert t.decision is not None
    assert t.decision.corrected.lines == [LineFix(sku="SEN-T100", quantity=2)]


def test_key_reuse_with_another_reply_is_refused(db: Settings, mails: list[EmailMessage]) -> None:
    ext = awaiting(db)
    dbos_async.run(
        service.approve(
            ext, Decision(version=0, operator="olga", reply=edited("A")), idempotency_key="k"
        )
    )
    with pytest.raises(service.IdempotencyMismatchError):
        dbos_async.run(
            service.approve(
                ext, Decision(version=0, operator="olga", reply=edited("B")), idempotency_key="k"
            )
        )


def test_stale_version_reports_the_current_one(db: Settings, mails: list[EmailMessage]) -> None:
    ext = awaiting(db)
    dbos_async.run(
        service.approve(
            ext, Decision(version=0, operator="olga", reply=edited("A")), idempotency_key="a"
        )
    )
    with pytest.raises(service.StaleVersionError) as exc:
        dbos_async.run(
            service.approve(
                ext, Decision(version=0, operator="olga", reply=edited("A")), idempotency_key="b"
            )
        )
    assert (exc.value.current_version, exc.value.status) == (2, "sent")


def test_shadow_mode_sends_nothing(
    db: Settings, mails: list[EmailMessage], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow, "current_policy", lambda: Policy(shadow=True))
    ext = awaiting(db)
    assert (
        dbos_async.run(
            service.approve(ext, Decision(version=0, operator="olga", reply=edited("A")))
        ).result
        == "shadowed"
    )
    assert mails == []


def test_escalate_then_resolve_keeps_the_escalation(
    db: Settings, mails: list[EmailMessage]
) -> None:
    ext = awaiting(db)
    dbos_async.run(
        service.escalate(ext, Escalation(version=0, operator="olga", escalate_to="sales"))
    )
    dbos_async.run(service.resolve(ext, Resolution(version=1, operator="ivan")))
    t = dbos_async.run(service.get_rfq(ext))
    assert t.status == "resolved"
    assert t.decision is not None
    assert t.decision.prior is not None
    assert t.decision.prior.corrected.escalate_to == "sales"
    assert mails == []


def test_resolve_a_quote_is_a_conflict(db: Settings) -> None:
    ext = awaiting(db)
    with pytest.raises(service.StaleVersionError):
        dbos_async.run(service.resolve(ext, Resolution(version=0, operator="olga")))


def test_unknown_request(db: Settings) -> None:
    with pytest.raises(service.NotFoundError):
        dbos_async.run(service.get_rfq("nope"))


@pytest.mark.parametrize(
    ("lines", "reason"),
    [
        ([LineFix(sku="SEN-T100", quantity=500)], "10000"),  # over the threshold: sales decides
        ([LineFix(sku="NOPE-1", quantity=5)], "NOPE-1"),  # not in the catalog
        ([], "no lines"),
    ],
)
def test_a_line_fix_that_is_not_a_plain_quote_is_refused(
    db: Settings, mails: list[EmailMessage], lines: list[LineFix], reason: str
) -> None:
    ext = awaiting(db)
    d = Decision(version=0, operator="olga", reply=QUOTE.draft or "", lines=lines)
    with pytest.raises(service.InvalidLinesError, match=reason):
        dbos_async.run(service.approve(ext, d))
    assert mails == []


def test_a_line_fix_updates_the_stored_quote_and_the_event(
    db: Settings, mails: list[EmailMessage]
) -> None:
    ext = awaiting(db)
    d = Decision(
        version=0,
        operator="olga",
        reply=QUOTE.draft or "",
        lines=[LineFix(sku="SEN-T100", quantity=2)],
    )
    dbos_async.run(service.approve(ext, d))
    t = dbos_async.run(service.get_rfq(ext))
    assert t.output is not None
    assert str(t.output.total) == "96.00"


def test_a_reply_that_changes_the_figures_is_refused(
    db: Settings, mails: list[EmailMessage]
) -> None:
    ext = awaiting(db)
    assert QUOTE.draft is not None
    cheap = QUOTE.draft.replace("= 576.00 EUR", "= 25.00 EUR")
    assert cheap != QUOTE.draft
    with pytest.raises(service.InvalidReplyError, match=r"through lines.*576\.00"):
        dbos_async.run(service.approve(ext, Decision(version=0, operator="olga", reply=cheap)))
    assert mails == []
    assert dbos_async.run(service.get_rfq(ext)).status == "awaiting_approval"


def test_words_around_the_figures_go_out_as_written(
    db: Settings, mails: list[EmailMessage]
) -> None:
    ext = awaiting(db)
    dbos_async.run(service.approve(ext, Decision(version=0, operator="olga", reply=edited("Hi"))))
    t = dbos_async.run(service.get_rfq(ext))
    assert t.reply == edited("Hi")
    assert t.decision is not None
    assert t.decision.edited_reply


def test_lines_without_a_reply_send_the_repriced_quote(
    db: Settings, mails: list[EmailMessage]
) -> None:
    ext = awaiting(db)
    d = Decision(version=0, operator="olga", lines=[LineFix(sku="SEN-T100", quantity=2)])
    dbos_async.run(service.approve(ext, d))
    t = dbos_async.run(service.get_rfq(ext))
    assert t.output is not None
    assert t.reply == t.output.draft
    assert "= 96.00 EUR" in (t.reply or "")
    assert t.decision is not None
    assert not t.decision.edited_reply


def test_lines_with_an_edited_reply_must_carry_the_new_figures(
    db: Settings, mails: list[EmailMessage]
) -> None:
    ext = awaiting(db)
    d = Decision(
        version=0,
        operator="olga",
        reply=edited("Hi"),  # still the old 12-unit figures
        lines=[LineFix(sku="SEN-T100", quantity=2)],
    )
    with pytest.raises(service.InvalidReplyError, match=r"96\.00"):
        dbos_async.run(service.approve(ext, d))
    assert mails == []


def test_status_all_lists_every_status(db: Settings) -> None:
    mine = {awaiting(db), awaiting(db, status="escalated")}
    listed = dbos_async.run(service.list_rfqs(status="all")).items
    assert mine <= {t.external_id for t in listed}
