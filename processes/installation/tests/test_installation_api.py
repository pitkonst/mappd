"""The dispatcher's API and jobs page."""

from collections.abc import Iterator
from uuid import uuid4

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mappd.channels.telegram import TelegramClient
from mappd.db.migrate import sqlalchemy_url
from mappd.settings import Settings
from processes.installation import api, pages, telegram, workflow
from processes.installation.db import METADATA

JOB = {
    "external_id": "j-1",
    "installer_name": "Ivan",
    "site": "Riverside",
    "skus": ["CTL-H400"],
    "appointment_at": "2026-10-16T09:00:00Z",
}


@pytest.fixture
def client(
    dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, list[str]]]:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    monkeypatch.setenv("MAPPD_INSTALLATION_BOT_NAME", "helio_bot")
    engine = sa.create_engine(sqlalchemy_url(dbos_runtime.database_url))
    try:
        METADATA.create_all(engine, checkfirst=True)
    finally:
        engine.dispose()
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE installation_deadlines, installation_messages, installation_jobs,"
            " installation_installers, installation_polling CASCADE"
        )
    sent: list[str] = []

    class Client(TelegramClient):
        def __init__(self) -> None:
            pass

        def send_message(self, chat_id: int, text: str) -> dict[str, object]:
            sent.append(text)
            return {}

    monkeypatch.setattr(telegram, "client", Client)
    app = FastAPI(lifespan=dbos_async.lifespan)
    app.state.processes = ["installation"]
    app.include_router(api.router)
    app.include_router(pages.router)
    with TestClient(app) as c:
        yield c, sent


def test_create_returns_a_deep_link_and_an_arrival_deadline(
    client: tuple[TestClient, list[str]], dbos_runtime: Settings
) -> None:
    c, _ = client
    r = c.post("/api/v1/installation/jobs", json=JOB)
    assert r.status_code == 201
    body = r.json()
    assert body["state"] == "assigned"
    assert body["deep_link"].startswith("https://t.me/helio_bot?start=")
    assert body["installer_name"] == JOB["installer_name"], "the name comes back as it went in"
    with psycopg.connect(dbos_runtime.database_url) as conn:
        row = conn.execute("SELECT kind, due_at FROM installation_deadlines").fetchone()
    assert row is not None
    assert row[0] == "arrival"
    assert c.post("/api/v1/installation/jobs", json=JOB).status_code == 409


def block(settings: Settings) -> None:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("UPDATE installation_installers SET chat_id = 77, link_token = NULL")
        conn.execute(
            "UPDATE installation_jobs SET state = 'blocked', blocked_from = 'access',"
            " blocked_reason = 'missing_parts', version = 3"
        )
        conn.execute(
            "INSERT INTO installation_deadlines (job_id, kind, due_at)"
            " SELECT id, 'dispatcher_response', now() + interval '2 hours' FROM installation_jobs"
        )


def test_unblock_returns_the_job_and_replies_once(
    client: tuple[TestClient, list[str]], dbos_runtime: Settings
) -> None:
    c, sent = client
    c.post("/api/v1/installation/jobs", json=JOB)
    block(dbos_runtime)
    body = {"version": 3, "operator": "olga"}
    r = c.post("/api/v1/installation/jobs/j-1/unblock", json=body)
    assert r.status_code == 200
    assert r.json()["state"] == "access"
    assert len(sent) == 1
    again = c.post("/api/v1/installation/jobs/j-1/unblock", json=body)
    assert again.status_code == 200, "the same decision again: a repeat, not a second unblock"
    assert len(sent) == 1
    stale = c.post("/api/v1/installation/jobs/j-1/unblock", json={"version": 2, "operator": "olga"})
    assert stale.status_code == 409
    with psycopg.connect(dbos_runtime.database_url) as conn:
        row = conn.execute(
            "SELECT settled_at IS NOT NULL FROM installation_deadlines"
            " WHERE kind = 'dispatcher_response'"
        ).fetchone()
    assert row == (True,)


def test_get_and_list(client: tuple[TestClient, list[str]]) -> None:
    c, _ = client
    c.post("/api/v1/installation/jobs", json=JOB)
    assert c.get("/api/v1/installation/jobs/j-1").json()["site"] == "Riverside"
    assert [j["external_id"] for j in c.get("/api/v1/installation/jobs").json()] == ["j-1"]
    assert c.get("/api/v1/installation/jobs/nope").status_code == 404


def test_page_lists_a_blocked_job_with_its_reason(
    client: tuple[TestClient, list[str]], dbos_runtime: Settings
) -> None:
    c, _ = client
    c.post("/api/v1/installation/jobs", json=JOB)
    block(dbos_runtime)
    html = c.get("/installation").text
    assert "Riverside" in html
    assert "missing parts" in html


def test_an_installer_has_one_open_job(
    client: tuple[TestClient, list[str]], dbos_runtime: Settings
) -> None:
    # Messages go to the installer's open job: a second one would take them over.
    c, _ = client
    assert c.post("/api/v1/installation/jobs", json=JOB).status_code == 201
    second = {**JOB, "external_id": "j-2"}
    r = c.post("/api/v1/installation/jobs", json=second)
    assert r.status_code == 409
    assert "open job" in r.json()["detail"]
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute("UPDATE installation_jobs SET state = 'handed_over'")
    assert c.post("/api/v1/installation/jobs", json=second).status_code == 201


def test_unblocking_withdraws_the_pending_proposal(
    client: tuple[TestClient, list[str]], dbos_runtime: Settings
) -> None:
    c, _ = client
    c.post("/api/v1/installation/jobs", json=JOB)
    block(dbos_runtime)
    version = 100 + uuid4().int % 10**6  # unblock workflows are keyed by job and version
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute("UPDATE installation_jobs SET version = %s", (version,))
        row = conn.execute(
            "INSERT INTO installation_proposals (job_id, tool, arguments, reason)"
            " SELECT id, 'reserve_parts', '{\"skus\": [\"CTL-H400\"]}', 'in stock'"
            " FROM installation_jobs RETURNING id"
        ).fetchone()
        assert row is not None
    pid = str(row[0])
    body = {"version": version, "operator": "olga"}
    assert c.post("/api/v1/installation/jobs/j-1/unblock", json=body).status_code == 200
    job = c.get("/api/v1/installation/jobs/j-1").json()
    assert [(p["status"], p["decided_by"]) for p in job["proposals"]] == [("withdrawn", "olga")]
    late = c.post(
        f"/api/v1/installation/jobs/j-1/proposals/{pid}/confirm", json={"operator": "olga"}
    )
    assert late.status_code == 409, "nothing is reserved for a job that moved on"
    events = c.get("/api/v1/installation/jobs/j-1/timeline").json()
    assert "withdrawn" in [e["event"] for e in events]


def test_the_unblock_reply_is_stored_as_the_bots_message(
    client: tuple[TestClient, list[str]], dbos_runtime: Settings
) -> None:
    # Its own job id: an unblock's workflow ID is (job, version), and DBOS replays a used one.
    c, _ = client
    c.post("/api/v1/installation/jobs", json={**JOB, "external_id": "j-reply"})
    block(dbos_runtime)
    c.post("/api/v1/installation/jobs/j-reply/unblock", json={"version": 3, "operator": "olga"})
    messages = c.get("/api/v1/installation/jobs/j-reply").json()["messages"]
    assert [(m["direction"], m["author"], m["text"]) for m in messages] == [
        ("out", "bot", workflow.UNBLOCKED)
    ]


def test_a_negative_version_is_refused(client: tuple[TestClient, list[str]]) -> None:
    c, _ = client
    c.post("/api/v1/installation/jobs", json={**JOB, "external_id": "j-neg"})
    r = c.post("/api/v1/installation/jobs/j-neg/unblock", json={"version": -1, "operator": "olga"})
    assert r.status_code == 422


def test_an_unblock_whose_reply_fails_still_answers_the_unblocked_job(
    client: tuple[TestClient, list[str]], dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx2  # noqa: PLC0415

    c, _ = client
    c.post("/api/v1/installation/jobs", json={**JOB, "external_id": "j-noreply"})
    block(dbos_runtime)

    def timeout(*_a: object, **_k: object) -> dict[str, object]:
        raise httpx2.ReadTimeout("slow")

    monkeypatch.setattr(telegram.client().__class__, "send_message", timeout)
    r = c.post(
        "/api/v1/installation/jobs/j-noreply/unblock", json={"version": 3, "operator": "olga"}
    )
    assert r.status_code == 200
    assert r.json()["state"] == "access"
