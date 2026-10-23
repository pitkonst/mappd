from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mappd.app import create_app
from mappd.process import Process
from mappd.settings import Settings
from mappd.web import pages

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def client(migrated: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MAPPD_DATABASE_URL", migrated.database_url)
    app = FastAPI()
    app.state.processes = []
    app.include_router(pages.router)
    return TestClient(app)


def test_actions_page_lists_unknown_and_resolves_a_row(
    client: TestClient, migrated: Settings
) -> None:
    key = f"demo.send:{uuid4()}"
    with psycopg.connect(migrated.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO actions (kind, idempotency_key, payload, status, last_error, claimed_by)"
            " VALUES ('demo.send', %s, '{}', 'unknown', 'timeout after send', 'wf-demo-1')",
            (key,),
        )
    html = client.get("/actions").text
    assert key in html
    assert "timeout after send" in html
    assert "took effect" in html
    assert "not sent" in html
    assert "confirm(" not in html
    nameless = client.post("/actions/resolve", data={"key": key, "took_effect": "false"})
    assert "Type your name" in nameless.text, "settling records who checked"
    assert "unknown" in nameless.text
    data = {"key": key, "took_effect": "false", "operator": "olga"}
    row = client.post("/actions/resolve", data=data)
    assert row.status_code == 200
    assert "<tr" in row.text
    assert "pending" in row.text
    assert "settled by olga" in row.text


def test_resolving_twice_answers_a_row_saying_so(client: TestClient) -> None:
    data = {"key": "nope", "took_effect": "true", "operator": "olga"}
    row = client.post("/actions/resolve", data=data)
    assert row.status_code == 200
    assert "no unknown action" in row.text


@pytest.mark.usefixtures("no_ops_db")
def test_index_links_process_dashboards(tmp_path: Path) -> None:
    app = create_app(
        Settings(processes_dir=tmp_path),
        processes=[Process(name="demo", dashboard="/demo/dashboard")],
    )
    html = TestClient(app).get("/").text
    assert 'href="/demo/dashboard"' in html
    assert '<a class="stat" href="/demo">' in html, "each process is a card"


def test_action_rows_link_their_workflow(client: TestClient, migrated: Settings) -> None:
    key = f"demo.send:{uuid4()}"
    with psycopg.connect(migrated.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO actions (kind, idempotency_key, payload, status, claimed_by)"
            " VALUES ('demo.send', %s, '{}', 'unknown', 'ticket/t-1 x')",
            (key,),
        )
    assert 'href="/workflows/ticket/t-1%20x"' in client.get("/actions").text


def test_a_webhook_row_reads_as_its_event() -> None:
    from mappd.web.pages import summary  # noqa: PLC0415

    payload = {
        "url_env": "MAPPD_SUPPORT_WEBHOOK_URL",
        "secret_env": "MAPPD_SUPPORT_WEBHOOK_SECRET",
        "event": {"type": "closed", "case_id": "mail-0123", "process": "support"},
    }
    assert summary(payload) == "type: closed · case_id: mail-0123"
