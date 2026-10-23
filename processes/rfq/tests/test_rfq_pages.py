"""RFQ pages: the queue, the request with its lines and forms, the dashboard."""

import re
from collections.abc import Iterator
from uuid import uuid4

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa
from fake_mail import install
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from mappd.db.migrate import sqlalchemy_url
from mappd.settings import Settings
from processes.rfq import domain, pages
from processes.rfq.db import METADATA
from processes.rfq.schemas import Extracted, Extraction

QUOTE = domain.solve_output(
    Extraction(
        lines=[Extracted(requested="sensors", quantity=12, sku="SEN-T100", confidence="high")],
        needed_by_days=None,
        custom_request=False,
        language="en",
    )
)


@pytest.fixture
def client(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    engine = sa.create_engine(sqlalchemy_url(dbos_runtime.database_url))
    try:
        METADATA.create_all(engine, checkfirst=True)
    finally:
        engine.dispose()
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM rfq_requests")
    install(monkeypatch, "rfq")
    app = FastAPI(lifespan=dbos_async.lifespan)
    app.state.processes = ["rfq"]
    app.include_router(pages.router)
    with TestClient(app) as c:
        yield c


def awaiting(settings: Settings) -> str:
    ext = f"pg-{uuid4().hex[:10]}"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO rfq_requests (external_id, sender, subject, body, status, output)"
            " VALUES (%s, 'anna@example.com', 'Quote please', '12 sensors',"
            " 'awaiting_approval', %s)",
            (ext, Jsonb(QUOTE.model_dump(mode="json"))),
        )
    return ext


def test_queue_lists_awaiting_requests(client: TestClient, dbos_runtime: Settings) -> None:
    ext = awaiting(dbos_runtime)
    html = client.get("/rfq").text
    assert ext in html
    assert 'data-live="/api/v1/rfq/events"' in html


def test_request_page_shows_lines_total_and_forms(
    client: TestClient, dbos_runtime: Settings
) -> None:
    ext = awaiting(dbos_runtime)
    html = client.get(f"/rfq/requests/{ext}").text
    assert "SEN-T100" in html
    assert "576.00" in html
    assert f'hx-post="/rfq/requests/{ext}/approve"' in html
    assert f'hx-post="/rfq/requests/{ext}/escalate"' in html
    assert 'name="reply"' in html


def test_approve_then_double_submit(client: TestClient, dbos_runtime: Settings) -> None:
    ext = awaiting(dbos_runtime)
    data = {"version": "0", "operator": "olga", "reply": QUOTE.draft or ""}
    first = client.post(
        f"/rfq/requests/{ext}/approve", data=data, headers={"Idempotency-Key": "r1"}
    )
    again = client.post(
        f"/rfq/requests/{ext}/approve", data=data, headers={"Idempotency-Key": "r1"}
    )
    assert "Sent." in first.text
    assert "Already sent" in again.text
    assert first.headers["HX-Refresh"] == "true", "the page reloads: no stale form or status"


def test_a_reply_with_other_figures_is_shown_as_a_field_error(
    client: TestClient, dbos_runtime: Settings
) -> None:
    ext = awaiting(dbos_runtime)
    reply = (QUOTE.draft or "").replace("EUR", "USD")
    data = {"version": "0", "operator": "olga", "reply": reply}
    r = client.post(f"/rfq/requests/{ext}/approve", data=data, headers={"Idempotency-Key": "f"})
    assert "Check the highlighted fields." in r.text
    assert r.headers["HX-Retarget"] == "#fields-approve", "the form's own fields are replaced"
    assert 'id="approve-reply"' in r.text, "the same ids as the fields it replaces"
    assert "through lines" in r.text
    assert 'name="lines"' in r.text, "lines stay hidden: the form has no widget for them"


def test_dashboard_links_open_the_queue(client: TestClient, dbos_runtime: Settings) -> None:
    awaiting(dbos_runtime)
    html = client.get("/rfq/dashboard").text
    links = sorted(set(re.findall(r'href="(/rfq\?[^"]*)"', html)))
    assert links
    for link in links:
        assert client.get(link.replace("&amp;", "&")).status_code == 200, link


def test_every_id_on_the_request_page_is_unique(client: TestClient, dbos_runtime: Settings) -> None:
    ext = awaiting(dbos_runtime)
    ids = re.findall(r'\sid="([^"]+)"', client.get(f"/rfq/requests/{ext}").text)
    assert {"approve-operator", "escalate-operator"} <= set(ids)
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)


def test_a_missing_name_and_a_price_line_show_together(
    client: TestClient, dbos_runtime: Settings
) -> None:
    ext = awaiting(dbos_runtime)
    reply = f"{QUOTE.draft}\n\nSpecial deal: 50% off."
    data = {"version": "0", "operator": "", "reply": reply}
    r = client.post(f"/rfq/requests/{ext}/approve", data=data, headers={"Idempotency-Key": "two"})
    assert "at least 1 character" in r.text, "the name"
    assert "a currency or a percentage" in r.text, "and the price line, in the same round"
