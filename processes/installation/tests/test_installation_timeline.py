"""The dispatcher's view of one job: its timeline, its photos, and the job page's actions."""

from collections.abc import Iterator
from typing import Any

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from mappd.channels.telegram import TelegramClient
from mappd.db.migrate import sqlalchemy_url
from mappd.settings import Settings
from processes.installation import api, pages, telegram
from processes.installation.db import METADATA

PHOTO = b"\xff\xd8 a jpeg"


@pytest.fixture
def client(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
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

    class Client(TelegramClient):
        def __init__(self) -> None:
            pass

        def get_file(self, file_id: str) -> bytes:
            return PHOTO

        def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
            return {"message_id": 1}

    monkeypatch.setattr(telegram, "client", Client)
    app = FastAPI(lifespan=dbos_async.lifespan)
    app.state.processes = ["installation"]
    app.include_router(api.router)
    app.include_router(pages.router)
    with TestClient(app) as c:
        yield c


def seed(settings: Settings, ext: str = "j-1", state: str = "blocked") -> int:
    """A job with an inbound photo, the bot's reply, events, and a pending proposal."""
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        installer = conn.execute(
            "INSERT INTO installation_installers (name, chat_id) VALUES (%s, %s) RETURNING id",
            (f"Ivan {ext}", abs(hash(ext)) % 10**9),
        ).fetchone()
        assert installer is not None
        job = conn.execute(
            "INSERT INTO installation_jobs (external_id, installer_id, site, skus,"
            " appointment_at, state, blocked_from, blocked_reason)"
            " VALUES (%s, %s, 'Riverside', %s, now(), %s, %s, %s) RETURNING id",
            (
                ext,
                installer[0],
                Jsonb(["CTL-H400"]),
                state,
                "access" if state == "blocked" else None,
                "missing_parts" if state == "blocked" else None,
            ),
        ).fetchone()
        assert job is not None
        jid = int(job[0])
        conn.execute(
            "INSERT INTO installation_messages (job_id, chat_id, direction, author, text,"
            " photo_ref, photo_description, created_at) VALUES"
            " (%s, 1, 'in', 'installer', 'no controller here', 'f-1', 'an empty box',"
            "  now() - interval '3 minutes'),"
            " (%s, 1, 'out', 'bot', 'A dispatcher will confirm.', NULL, NULL,"
            "  now() - interval '1 minute')",
            (jid, jid),
        )
        conn.execute(
            "INSERT INTO installation_events (job_id, kind, actor, data, created_at) VALUES"
            " (%s, 'blocked', 'agent', %s, now() - interval '2 minutes'),"
            " (%s, 'proposal', 'agent', %s, now() - interval '2 minutes')",
            (jid, Jsonb({"to": "blocked"}), jid, Jsonb({"tool": "reserve_parts"})),
        )
        conn.execute(
            "INSERT INTO installation_proposals (job_id, tool, arguments, reason)"
            " VALUES (%s, 'reserve_parts', %s, 'in stock')",
            (jid, Jsonb({"job": ext, "skus": ["CTL-H400"]})),
        )
    return jid


def test_the_timeline_merges_messages_and_events_in_order(
    client: TestClient, dbos_runtime: Settings
) -> None:
    seed(dbos_runtime)
    entries = client.get("/api/v1/installation/jobs/j-1/timeline").json()
    assert [(e["kind"], e["actor"], e["event"]) for e in entries] == [
        ("message", "installer", None),
        ("event", "agent", "blocked"),
        ("event", "agent", "proposal"),
        ("message", "bot", None),
    ]
    assert entries[0]["photo"] == "f-1"
    assert entries[0]["photo_description"] == "an empty box"
    assert client.get("/api/v1/installation/jobs/nope/timeline").status_code == 404


def test_a_reply_follows_its_message_when_another_came_in_between(
    client: TestClient, dbos_runtime: Settings
) -> None:
    jid = seed(dbos_runtime)
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM installation_events")
        conn.execute("DELETE FROM installation_messages")

        def said(author: str, text: str, ago: int, reply_to: int | None = None) -> int:
            row = conn.execute(
                "INSERT INTO installation_messages"
                " (job_id, chat_id, direction, author, text, reply_to, created_at)"
                " VALUES (%s, 7, %s, %s, %s, %s, now() - make_interval(secs => %s)) RETURNING id",
                (jid, "in" if author == "installer" else "out", author, text, reply_to, ago),
            ).fetchone()
            assert row is not None
            return int(row[0])

        first = said("installer", "arrived", 50)
        said("installer", "access granted", 40)  # before the first one was answered
        conn.execute(
            "INSERT INTO installation_events (job_id, kind, actor, message_id, created_at)"
            " VALUES (%s, 'transition', 'agent', %s, now() - interval '30 seconds')",
            (jid, first),
        )
        said("bot", "on site, noted", 20, reply_to=first)
    entries = client.get("/api/v1/installation/jobs/j-1/timeline").json()
    assert [e["text"] or e["event"] for e in entries] == [
        "arrived",
        "transition",
        "on site, noted",
        "access granted",
    ]


def test_the_photo_proxy_serves_only_the_jobs_own_photos(
    client: TestClient, dbos_runtime: Settings
) -> None:
    seed(dbos_runtime)
    seed(dbos_runtime, ext="j-2")
    ok = client.get("/api/v1/installation/jobs/j-1/photos/f-1")
    assert (ok.status_code, ok.content, ok.headers["content-type"]) == (200, PHOTO, "image/jpeg")
    assert client.get("/api/v1/installation/jobs/j-1/photos/f-other").status_code == 404
    assert client.get("/api/v1/installation/jobs/nope/photos/f-1").status_code == 404


def test_the_job_page_offers_the_actions_its_state_allows(
    client: TestClient, dbos_runtime: Settings
) -> None:
    seed(dbos_runtime)
    page = client.get("/installation/jobs/j-1").text
    assert "no controller here" in page
    assert 'src="/api/v1/installation/jobs/j-1/photos/f-1"' in page
    assert "Take over" in page
    assert "Unblock" in page
    assert "Confirm" in page
    assert "Reject" in page
    assert "Release" not in page
    held = client.post("/installation/jobs/j-1/takeover", data={"operator": "olga", "version": 0})
    assert held.status_code == 200
    assert "Release" in held.text
    assert 'name="text"' in held.text, "a send box while held"
    assert client.get("/installation/jobs/nope").status_code == 404


def test_the_jobs_page_links_each_job(client: TestClient, dbos_runtime: Settings) -> None:
    seed(dbos_runtime)
    assert 'href="/installation/jobs/j-1"' in client.get("/installation").text


def test_a_page_action_on_a_stale_version_shows_the_conflict(
    client: TestClient, dbos_runtime: Settings
) -> None:
    seed(dbos_runtime)
    stale = client.post("/installation/jobs/j-1/takeover", data={"operator": "olga", "version": 9})
    assert stale.status_code == 409
    assert "changed" in stale.text


@pytest.mark.parametrize("action", ["takeover", "unblock", "advance"])
def test_a_page_action_without_a_dispatcher_is_refused(
    client: TestClient, dbos_runtime: Settings, action: str
) -> None:
    seed(dbos_runtime)
    r = client.post(f"/installation/jobs/j-1/{action}", data={"operator": " ", "version": 0})
    assert r.status_code == 422
    assert "dispatcher" in r.text
    assert client.get("/api/v1/installation/jobs/j-1").json()["version"] == 0


def test_a_photo_telegram_cannot_serve_is_a_bad_gateway(
    client: TestClient, dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mappd.channels.telegram import TelegramError  # noqa: PLC0415

    seed(dbos_runtime)

    def unavailable(*_a: object, **_k: object) -> bytes:
        raise TelegramError("getFile: Unauthorized")

    monkeypatch.setattr(telegram.client().__class__, "get_file", unavailable)
    r = client.get("/api/v1/installation/jobs/j-1/photos/f-1")
    assert r.status_code == 502
    assert "Unauthorized" in r.json()["detail"]


def test_the_page_listens_to_its_own_job_and_refreshes_without_the_forms(
    client: TestClient, dbos_runtime: Settings
) -> None:
    from mappd.events import Event  # noqa: PLC0415

    seed(dbos_runtime)
    page = client.get("/installation/jobs/j-1").text
    assert 'data-live="/api/v1/installation/events?job=j-1"' in page
    client.post("/installation/jobs/j-1/takeover", data={"operator": "olga", "version": 0})
    live = client.get("/installation/jobs/j-1/live").text
    assert "no controller here" in live
    assert "<form" not in live, "a refresh never wipes what the dispatcher is typing"
    assert '<span class="kv"><span>tool</span> reserve_parts</span>' in live, "fields, not JSON"
    assert '{"tool"' not in live, "no raw JSON for flat event data"
    mine = Event.of("installation", "j-1", "message", 1)
    theirs = Event.of("installation", "j-2", "message", 1)
    assert api.wanted(mine, "j-1")
    assert not api.wanted(theirs, "j-1")
    assert api.wanted(theirs, None)


def test_the_jobs_list_opens_on_what_needs_a_dispatcher(
    client: TestClient, dbos_runtime: Settings
) -> None:
    seed(dbos_runtime, "j-1")  # blocked, with a pending proposal
    seed(dbos_runtime, "j-2", state="handed_over")
    needs = client.get("/installation").text
    assert 'href="/installation/jobs/j-1"' in needs
    assert 'href="/installation/jobs/j-2"' not in needs
    assert "1 proposal to decide" in needs
    done = client.get("/installation", params={"tab": "handed_over"}).text
    assert 'href="/installation/jobs/j-2"' in done
    assert 'href="/installation/jobs/j-1"' not in done


def test_the_job_page_shows_the_link_an_unlinked_installer_needs(
    client: TestClient, dbos_runtime: Settings
) -> None:
    seed(dbos_runtime)
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute("UPDATE installation_installers SET chat_id = NULL, link_token = 'tok-x'")
    page = client.get("/installation/jobs/j-1").text
    assert "has not linked Telegram yet" in page
    assert "start=tok-x" in page


def test_ops_and_the_jobs_page_count_the_same_jobs(
    client: TestClient, dbos_runtime: Settings
) -> None:
    from mappd import db, ops  # noqa: PLC0415
    from processes.installation import process  # noqa: PLC0415

    seed(dbos_runtime, "j-1")  # blocked, with a pending proposal
    late = seed(dbos_runtime, "j-2", state="assigned")  # its proposal is pending too
    seed(dbos_runtime, "j-3", state="handed_over")  # finished: never needs anyone
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM installation_proposals WHERE job_id = %s", (late,))
        conn.execute(
            "INSERT INTO installation_deadlines (job_id, kind, due_at)"
            " VALUES (%s, 'arrival', now() - interval '1 hour')",
            (late,),
        )
    assert process.cases is not None
    with db.connect() as conn:
        waiting = ops.process_ops(conn, "installation", process.cases).waiting
    page = client.get("/installation").text
    assert waiting == 2
    assert "needs you <small>2</small>" in page
