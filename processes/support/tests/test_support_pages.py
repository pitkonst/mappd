"""Support pages: the queue, the ticket with its generated form, the approval answers."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import quote
from uuid import uuid4

import dbos_async
import httpx2
import psycopg
import pytest
import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID
from fake_mail import install
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from mappd import mail
from mappd.autonomy import Policy
from mappd.db.migrate import sqlalchemy_url
from mappd.settings import Settings
from processes.support import domain, pages, workflow
from processes.support.db import METADATA

OUTPUT = {
    "topic": "malfunction",
    "route": "technical",
    "urgency": "normal",
    "language": "en",
    "outcome": "reply",
    "escalate_to": None,
    "draft": {"reply": "Power-cycle the controller.", "citations": ["kb-101"]},
}


@pytest.fixture
def client(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
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
        conn.execute("DELETE FROM support_kb")
        conn.execute(
            "INSERT INTO support_kb (id, title, body, embedding)"
            " VALUES ('kb-101', 'Controller offline', 'Power-cycle it.', %s)",
            ("[" + ",".join(["0"] * 64) + "]",),
        )
    install(monkeypatch, "support")
    # One loop for the whole test: a TestClient without `with` closes a loop per request,
    # and DBOS's thread pool closes with it (see dbos_async).
    app = FastAPI(lifespan=dbos_async.lifespan)
    app.state.processes = ["support"]
    app.include_router(pages.router)
    with TestClient(app) as c:
        yield c


def awaiting(settings: Settings, status: str = "awaiting_approval") -> str:
    ext = f"pg-{uuid4().hex[:10]}"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO support_tickets (external_id, sender, subject, body, status, output)"
            " VALUES (%s, 'anna@example.com', 'Controller offline', 'It is down.', %s, %s)",
            (ext, status, Jsonb(OUTPUT)),
        )
    return ext


def test_queue_lists_awaiting_tickets_and_listens_for_events(
    client: TestClient, dbos_runtime: Settings
) -> None:
    ext = awaiting(dbos_runtime)
    html = client.get("/support").text
    assert ext in html
    assert "Controller offline" in html
    assert 'data-live="/api/v1/support/events"' in html
    assert "<small>technical</small>" in html, "no team yet: the team column shows the route"
    assert 'hx-trigger="live"' in html
    rows = client.get("/support/rows", params={"status": "escalated"}).text
    assert ext not in rows


def test_ticket_page_renders_the_generated_form(client: TestClient, dbos_runtime: Settings) -> None:
    ext = awaiting(dbos_runtime)
    html = client.get(f"/support/tickets/{ext}").text
    assert "Power-cycle the controller." in html, "the draft prefills the reply"
    assert '<input type="hidden" name="version" value="0">' in html
    assert 'name="operator"' in html
    assert "<code>kb-101</code> Controller offline" in html, "cited article with its title"
    assert '"Idempotency-Key"' in html


def test_every_id_on_the_ticket_page_is_unique(client: TestClient, dbos_runtime: Settings) -> None:
    ext = awaiting(dbos_runtime)
    ids = re.findall(r'\sid="([^"]+)"', client.get(f"/support/tickets/{ext}").text)
    assert "approve-operator" in ids
    assert "escalate-operator" in ids
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)


def test_pages_mark_status_outcome_and_verdict(client: TestClient, dbos_runtime: Settings) -> None:
    ext = awaiting(dbos_runtime)
    rows = client.get("/support/rows").text
    assert '<span class="badge info">awaiting approval</span>' in rows, "undecided: the status"
    html = client.get(f"/support/tickets/{ext}").text
    assert '<span class="badge info">awaiting approval</span>' in html
    assert '<span class="badge ok">reply</span>' in html, "the agent's outcome"
    assert '<button class="primary">Approve and send</button>' in html
    dashboard = client.get("/support/dashboard").text
    assert 'class="stats"' in dashboard


def test_unknown_ticket_page_is_404(client: TestClient) -> None:
    assert client.get("/support/tickets/nope").status_code == 404


def post(client: TestClient, ext: str, key: str, version: int = 0) -> httpx2.Response:
    return client.post(
        f"/support/tickets/{ext}/approve",
        data={"version": str(version), "operator": "olga", "reply": "Done."},
        headers={"Idempotency-Key": key},
    )


def test_approve_then_double_submit_says_already_sent(
    client: TestClient, dbos_runtime: Settings
) -> None:
    ext = awaiting(dbos_runtime)
    first = post(client, ext, "render-1")
    assert first.status_code == 200
    assert "Sent" in first.text
    again = post(client, ext, "render-1")
    assert again.status_code == 200
    assert "Already sent" in again.text


def test_stale_render_says_the_ticket_changed(client: TestClient, dbos_runtime: Settings) -> None:
    ext = awaiting(dbos_runtime)
    post(client, ext, "render-a")
    stale = post(client, ext, "render-b")
    assert stale.status_code == 200
    assert "The ticket changed" in stale.text
    assert f"/support/tickets/{ext}" in stale.text


def test_invalid_form_shows_field_errors(client: TestClient, dbos_runtime: Settings) -> None:
    ext = awaiting(dbos_runtime)
    r = client.post(
        f"/support/tickets/{ext}/approve",
        data={"version": "x", "operator": "olga", "reply": "Done."},
        headers={"Idempotency-Key": "render-x"},
    )
    assert r.status_code == 200
    assert 'class="error"' in r.text


def test_a_blank_operator_is_refused_like_the_api_refuses_it(
    client: TestClient, dbos_runtime: Settings
) -> None:
    ext = awaiting(dbos_runtime)
    r = client.post(
        f"/support/tickets/{ext}/approve",
        data={"version": "0", "operator": " ", "reply": "Done."},
        headers={"Idempotency-Key": "blank-operator"},
    )
    assert 'class="error"' in r.text
    assert r.headers["HX-Retarget"] == "#fields-approve", "corrected where the error is shown"
    assert "/approve" in client.get(f"/support/tickets/{ext}").text, "still awaiting approval"


def test_a_failed_send_says_so_and_the_ticket_page_points_to_actions(
    client: TestClient, dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = awaiting(dbos_runtime)

    async def mailer_down(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("mailer down")

    monkeypatch.setattr(mail, "send", mailer_down)
    r = post(client, ext, "render-f")
    assert r.status_code == 200
    assert "Sending did not complete" in r.text
    assert "settling retries" in r.text
    page = client.get(f"/support/tickets/{ext}").text
    assert "Approved by olga" in page
    assert 'href="/actions?status=unknown"' in page
    assert "settling finishes this ticket" in page


def test_shadow_mode_says_nothing_was_sent(
    client: TestClient, dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow, "current_policy", lambda: Policy(shadow=True))
    ext = awaiting(dbos_runtime)
    r = post(client, ext, "render-sh")
    assert "nothing was sent" in r.text
    assert "shadow mode: not sent" in client.get(f"/support/tickets/{ext}").text


def test_awaiting_ticket_offers_approve_and_escalate(
    client: TestClient, dbos_runtime: Settings
) -> None:
    ext = awaiting(dbos_runtime)
    html = client.get(f"/support/tickets/{ext}").text
    assert f'hx-post="/support/tickets/{ext}/approve"' in html
    assert f'hx-post="/support/tickets/{ext}/escalate"' in html
    assert 'name="escalate_to"' in html
    assert 'name="topic"' in html


def test_escalated_ticket_offers_resolution(client: TestClient, dbos_runtime: Settings) -> None:
    ext = awaiting(dbos_runtime, status="escalated")
    html = client.get(f"/support/tickets/{ext}").text
    assert f'hx-post="/support/tickets/{ext}/resolve"' in html
    assert 'name="agent_could_handle"' in html
    r = client.post(
        f"/support/tickets/{ext}/resolve",
        data={"version": "0", "operator": "olga", "agent_could_handle": "true", "outcome": "reply"},
        headers={"Idempotency-Key": "res-1"},
    )
    assert "Resolved" in r.text


def test_escalating_from_the_page(client: TestClient, dbos_runtime: Settings) -> None:
    ext = awaiting(dbos_runtime)
    r = client.post(
        f"/support/tickets/{ext}/escalate",
        data={"version": "0", "operator": "olga", "escalate_to": "billing", "note": ""},
        headers={"Idempotency-Key": "esc-1"},
    )
    assert "Escalated to billing" in r.text


def test_queue_has_a_resolved_tab_and_passes_filters(client: TestClient) -> None:
    html = client.get("/support", params={"status": "resolved", "category": "how_to"}).text
    assert 'href="/support?status=resolved&amp;category=how_to"' in html, "tabs keep filters"
    assert "/support/rows?status=resolved&amp;category=how_to" in html


def test_dashboard_shows_rates_and_links_to_cases(client: TestClient) -> None:
    html = client.get("/support/dashboard").text
    assert "approved without edits" in html
    assert "false escalations" in html
    assert "/support?status=" in html


def test_dashboard_links_open_the_queue(client: TestClient, dbos_runtime: Settings) -> None:
    ext = awaiting(dbos_runtime)
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE support_tickets SET status = 'shadowed', decision = %s WHERE external_id = %s",
            (
                Jsonb(
                    {
                        "kind": "send",
                        "operator": "auto",
                        "shadow": True,
                        "corrected": {},
                        "decided_at": datetime.now(UTC).isoformat(),
                    }
                ),
                ext,
            ),
        )
    html = client.get("/support/dashboard").text
    links = sorted(set(re.findall(r'href="(/support\?[^"]*)"', html)))
    assert links
    for link in links:
        assert client.get(link.replace("&amp;", "&")).status_code == 200, link
    auto = next(link for link in links if "decision=auto" in link)
    assert ext in client.get(auto.replace("&amp;", "&")).text, "shadowed auto decisions are listed"


def test_queue_rows_show_whether_a_reply_really_went_out(
    client: TestClient, dbos_runtime: Settings
) -> None:
    decided = {"kind": "send", "operator": "dana", "decided_at": "2026-10-23T10:00:00Z"}
    escalated_by_operator = {
        "kind": "resolve",
        "operator": "dana",
        "agent_could_handle": True,
        "decided_at": "2026-10-23T10:00:00Z",
        "prior": {"kind": "escalate", "operator": "olga", "decided_at": "2026-10-23T09:00:00Z"},
    }
    for status, decision in (
        ("sent", decided),
        ("shadowed", decided),
        ("approved", decided),
        ("resolved", escalated_by_operator),
    ):
        ext = awaiting(dbos_runtime, status=status)
        with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
            conn.execute(
                "UPDATE support_tickets SET decision = %s WHERE external_id = %s",
                (Jsonb(decision), ext),
            )
    rows = client.get("/support/rows", params={"status": "all"}).text
    assert ">sent<" in rows
    assert "shadow: not sent" in rows
    assert "not confirmed sent" in rows
    assert "send by" not in rows, "the decision never stands in for the status"
    assert "false escalation" not in rows, "an operator's own escalation is not the agent's"


def test_the_ticket_page_lists_every_decision(client: TestClient, dbos_runtime: Settings) -> None:
    ext = awaiting(dbos_runtime, status="resolved")
    record = {
        "kind": "resolve",
        "operator": "dana",
        "decided_at": "2026-10-23T10:00:00Z",
        "note": "handled by phone",
        "prior": {
            "kind": "escalate",
            "operator": "olga",
            "decided_at": "2026-10-23T09:00:00Z",
            "corrected": {"escalate_to": "billing"},
        },
    }
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE support_tickets SET decision = %s WHERE external_id = %s",
            (Jsonb(record), ext),
        )
    html = client.get(f"/support/tickets/{ext}").text
    assert "<strong>resolve</strong> by dana" in html
    assert "<strong>escalate</strong> by olga" in html, "the earlier escalation stays visible"
    assert html.index("<strong>escalate</strong>") < html.index("<strong>resolve</strong>"), (
        "oldest first, as it happened"
    )
    assert "escalate to → billing" in html


@DBOS.workflow(name="test.support.stops")
def stops() -> None:
    raise RuntimeError("the classifier timed out")


def test_a_ticket_whose_processing_stopped_points_to_its_workflow(
    client: TestClient, dbos_runtime: Settings
) -> None:
    ext = awaiting(dbos_runtime, status="new")
    wid = domain.ticket_workflow_id(ext)
    with SetWorkflowID(wid), pytest.raises(RuntimeError):
        DBOS.start_workflow(stops).get_result()
    html = client.get(f"/support/tickets/{ext}").text
    assert "Processing stopped: its workflow failed" in html
    assert "retry it</a>" in html
    assert quote(wid, safe="") in html or wid in html, "it links the workflow"
