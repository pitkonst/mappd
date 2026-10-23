"""The Operations view: waiting and stuck cases per process, and the outbox's trouble."""

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from psycopg import sql

from mappd import db, ops
from mappd.app import create_app
from mappd.process import Process
from mappd.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mappd.db import Connection


def _cases_table(name: str) -> sa.Table:
    return sa.Table(
        name,
        sa.MetaData(),
        sa.Column("external_id", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


@pytest.fixture
def cases(migrated: Settings) -> Iterator[ops.Cases]:
    """A throwaway cases table: two waiting, one stuck in each passing status, the rest fine."""
    table = _cases_table(f"ops_cases_{uuid4().hex[:8]}")
    rows = [
        ("w-old", "awaiting_approval", "3 hours"),
        ("w-new", "awaiting_approval", "5 minutes"),
        ("e-1", "escalated", "1 hour"),
        ("n-stuck", "new", "20 minutes"),
        ("n-fresh", "new", "1 minute"),
        ("a-stuck", "approved", "2 hours"),
        ("done", "sent", "2 days"),
    ]
    name = sql.Identifier(table.name)
    with db.connect(migrated) as conn:
        conn.execute(
            sql.SQL(
                "CREATE TABLE {} (external_id text PRIMARY KEY, status text NOT NULL,"
                " updated_at timestamptz NOT NULL)"
            ).format(name)
        )
        for key, status, ago in rows:
            conn.execute(
                sql.SQL("INSERT INTO {} VALUES (%s, %s, now() - %s::interval)").format(name),
                (key, status, ago),
            )
    yield ops.Cases(
        table=table,
        link="/demo/cases/{key}",
        waiting=("awaiting_approval", "escalated"),
        passing={"new": timedelta(minutes=10), "approved": timedelta(minutes=10)},
    )
    with db.connect(migrated) as conn:
        conn.execute(sql.SQL("DROP TABLE {}").format(name))


def test_waiting_and_stuck_cases_of_a_process(migrated: Settings, cases: ops.Cases) -> None:
    with db.connect(migrated) as conn:
        got = ops.process_ops(conn, "demo", cases)
        t = cases.table
        query = sa.select(t.c.updated_at).where(t.c.external_id == "w-old")
        oldest = db.run(conn, query).fetchone()
    assert got.waiting == 3
    assert oldest is not None
    assert got.oldest_waiting == oldest["updated_at"]
    assert got.stuck == 2
    assert [(c.key, c.status) for c in got.stuck_cases] == [
        ("a-stuck", "approved"),
        ("n-stuck", "new"),
    ], "oldest first; a fresh `new` is not stuck"
    assert got.stuck_cases[0].link == "/demo/cases/a-stuck"


def test_stuck_cases_are_limited_but_counted(migrated: Settings, cases: ops.Cases) -> None:
    with db.connect(migrated) as conn:
        got = ops.process_ops(conn, "demo", cases, limit=1)
    assert got.stuck == 2
    assert [c.key for c in got.stuck_cases] == ["a-stuck"]


def test_no_passing_statuses_means_nothing_is_stuck(migrated: Settings, cases: ops.Cases) -> None:
    quiet = ops.Cases(table=cases.table, link=cases.link, waiting=("escalated",))
    with db.connect(migrated) as conn:
        got = ops.process_ops(conn, "demo", quiet)
    assert (got.waiting, got.stuck, got.stuck_cases) == (1, 0, [])


def _insert_action(conn: Connection, kind: str, status: str, created_ago: str) -> None:
    conn.execute(
        "INSERT INTO actions (kind, idempotency_key, payload, status, created_at)"
        " VALUES (%s, %s, '{}', %s, now() - %s::interval)",
        (kind, f"ops-{uuid4().hex}", status, created_ago),
    )


def test_outbox_counts_unknown_failed_and_webhooks_apart(migrated: Settings) -> None:
    with db.connect(migrated) as conn:
        before = ops.outbox_ops(conn)
        _insert_action(conn, "demo.send", "unknown", "1 minute")
        _insert_action(conn, "demo.send", "failed", "1 minute")
        _insert_action(conn, "demo.send", "done", "1 minute")
        _insert_action(conn, "mappd.send_webhook", "failed", "1 hour")
        _insert_action(conn, "mappd.send_webhook", "failed", "25 hours")
        _insert_action(conn, "mappd.send_webhook", "failed", "30 hours")
        after = ops.outbox_ops(conn)
    assert after.unknown - before.unknown == 1
    assert after.failed - before.failed == 1, "a failing webhook is the sweep's, not failed"
    assert after.webhooks_redelivering - before.webhooks_redelivering == 1
    assert after.webhooks_given_up - before.webhooks_given_up == 2


def _report(stuck: int = 0, unknown: int = 0, failed: int = 0, given_up: int = 0) -> ops.OpsReport:
    return ops.OpsReport(
        processes=[
            ops.ProcessOps(
                process="demo", waiting=4, oldest_waiting=None, stuck=stuck, stuck_cases=[]
            )
        ],
        outbox=ops.OutboxOps(
            unknown=unknown, failed=failed, webhooks_redelivering=3, webhooks_given_up=given_up
        ),
    )


def test_summary_names_only_what_needs_a_look() -> None:
    assert ops.summary(_report()) == "nothing stuck", "waiting and redelivering are normal"
    assert ops.summary(_report(stuck=1)) == "1 stuck case — see /ops"
    assert (
        ops.summary(_report(stuck=2, unknown=1, failed=3, given_up=2))
        == "2 stuck cases, 1 unknown action, 3 failed actions, 2 webhooks given up — see /ops"
    )


def test_cases_must_name_real_columns_and_a_key_in_the_link() -> None:
    table = _cases_table("t")
    with pytest.raises(ValueError, match="no column 'state'"):
        ops.Cases(table=table, link="/x/{key}", status="state")
    with pytest.raises(ValueError, match="placeholder"):
        ops.Cases(table=table, link="/x/")


def test_api_and_page_report_declaring_processes_only(
    migrated: Settings, cases: ops.Cases, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAPPD_DATABASE_URL", migrated.database_url)
    settings = Settings(processes_dir=tmp_path, database_url=migrated.database_url)
    app = create_app(settings, [Process(name="demo", cases=cases), Process(name="plain")])
    client = TestClient(app)
    body = client.get("/api/v1/ops").json()
    assert [p["process"] for p in body["processes"]] == ["demo"]
    assert body["processes"][0]["stuck"] == 2
    assert set(body["outbox"]) == {
        "unknown",
        "failed",
        "webhooks_redelivering",
        "webhooks_given_up",
    }
    assert set(body["workflows"]) == {"failed", "stranded"}, "failed workflows count too"
    page = client.get("/ops")
    assert page.status_code == 200
    assert 'href="/demo/cases/n-stuck"' in page.text
    assert "failed in the last 24 hours" in page.text


def test_every_demo_process_declares_its_cases() -> None:
    from processes.installation import process as installation  # noqa: PLC0415
    from processes.rfq import process as rfq  # noqa: PLC0415
    from processes.support import process as support  # noqa: PLC0415

    declared = {p.name: p.cases for p in (support, rfq, installation)}
    assert all(declared.values()), "a demo process missing from /ops"
    assert declared["installation"] is not None
    assert declared["installation"].waiting == ("blocked",)
    for name in ("support", "rfq"):
        cases = declared[name]
        assert cases is not None
        assert set(cases.waiting) == {"awaiting_approval", "escalated"}
        assert set(cases.passing) == {"new", "approved"}
