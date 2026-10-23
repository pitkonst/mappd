"""Trust numbers: rates and verdicts (pure), and the per-category query (Postgres)."""

from datetime import UTC, datetime
from uuid import uuid4

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa
from psycopg.types.json import Jsonb

from mappd.autonomy import Policy, Readiness
from mappd.db.migrate import sqlalchemy_url
from mappd.settings import Settings
from processes.support import trust
from processes.support.db import METADATA
from processes.support.trust import rates

READY = Readiness(without_edits=0.9, false_escalations_max=0.1, min_cases=3)


def row(**kw: int) -> dict[str, int]:
    return {
        "proposals": 0,
        "auto": 0,
        "without_edits": 0,
        "resolutions": 0,
        "false_escalations": 0,
        **kw,
    }


def test_ready_when_both_metrics_are_healthy() -> None:
    t = rates(row(proposals=10, without_edits=10, resolutions=4), "review", READY, "how_to")
    assert (t.without_edits_rate, t.false_escalation_rate) == (1.0, 0.0)
    assert t.verdict == "ready for auto"
    assert t.command == "mappd process autonomy support how_to auto"


def test_false_escalations_block_readiness() -> None:
    t = rates(
        row(proposals=10, without_edits=10, resolutions=4, false_escalations=2),
        "review",
        READY,
        "how_to",
    )
    assert (t.verdict, t.command) == ("keep review", None)


def test_too_few_cases_and_zero_denominators() -> None:
    t = rates(row(proposals=2, without_edits=2), "review", READY, "billing")
    assert t.verdict == "too few cases"
    assert t.false_escalation_rate is None


def test_auto_categories_show_auto() -> None:
    assert (
        rates(row(proposals=10, auto=10, without_edits=10), "auto", READY, "how_to").verdict
        == "auto"
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
    return dbos_runtime


def put(settings: Settings, topic: str, status: str, record: dict[str, object]) -> None:
    output = {
        "topic": topic,
        "route": "technical",
        "urgency": "low",
        "language": "en",
        "outcome": "reply",
        "escalate_to": None,
        "draft": None,
    }
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO support_tickets"
            " (external_id, sender, subject, body, status, output, decision)"
            " VALUES (%s, 'anna@example.com', 's', 'b', %s, %s, %s)",
            (f"tr-{uuid4().hex[:10]}", status, Jsonb(output), Jsonb(record)),
        )


def test_report_counts_per_category(db: Settings) -> None:
    recent = datetime.now(UTC).isoformat()
    put(
        db,
        "how_to",
        "sent",
        {"kind": "send", "operator": "auto", "corrected": {}, "decided_at": recent},
    )
    put(
        db,
        "how_to",
        "sent",
        {
            "kind": "send",
            "operator": "olga",
            "edited_reply": True,
            "corrected": {},
            "decided_at": recent,
        },
    )
    put(
        db,
        "how_to",
        "escalated",
        {
            "kind": "escalate",
            "operator": "olga",
            "corrected": {"outcome": "escalate"},
            "decided_at": recent,
        },
    )
    put(
        db,
        "billing",
        "resolved",
        {
            "kind": "resolve",
            "operator": "olga",
            "agent_could_handle": True,
            "corrected": {},
            "decided_at": recent,
        },
    )
    put(
        db,
        "billing",
        "sent",
        {"kind": "send", "operator": "olga", "corrected": {}, "decided_at": "2020-01-01T00:00:00Z"},
    )  # outside the window
    report = dbos_async.run(trust.report(30, Policy(ready=READY)))
    by = {c.category: c for c in report.categories}
    assert (by["how_to"].proposals, by["how_to"].auto, by["how_to"].without_edits) == (3, 1, 1)
    assert (by["billing"].resolutions, by["billing"].false_escalations) == (1, 1)
    assert report.total.proposals == 3
