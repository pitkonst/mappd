"""A dispatcher takes over the conversation: the agent stays silent until release."""

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import dbos_async
import httpx2
import psycopg
import pytest
import sqlalchemy as sa
from dbos import DBOS, WorkflowHandleAsync
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from mappd.channels.telegram import TelegramClient
from mappd.db.migrate import sqlalchemy_url
from mappd.settings import Settings
from processes.installation import api, domain, intake, service, telegram, workflow
from processes.installation.db import METADATA
from processes.installation.schemas import Interpretation, State


class Bot:
    """A fake Bot API: records what the bot sends; every file is a tiny JPEG."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/sendMessage"):
            with self.lock:
                self.sent.append(json.loads(request.content))
            return httpx2.Response(200, json={"ok": True, "result": {"message_id": 1}})
        if request.url.path.endswith("/getFile"):
            return httpx2.Response(200, json={"ok": True, "result": {"file_path": "p/1.jpg"}})
        return httpx2.Response(200, content=b"\xff\xd8JPEG")


@pytest.fixture
def bot(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> Bot:
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
    fake = Bot()
    http = httpx2.Client(transport=httpx2.MockTransport(fake.handle))
    monkeypatch.setattr(telegram, "client", lambda: TelegramClient("tok", "http://tg", client=http))
    monkeypatch.setattr(workflow, "describe_photo_text", lambda _image: "a cracked controller")

    async def interpret(
        state: State, nxt: State | None, text: str, photo: str | None
    ) -> Interpretation:
        return Interpretation(
            intent="progress", reached_next="done" in text, exception=None, reply="ok"
        )

    monkeypatch.setattr(workflow, "interpret", interpret)
    return fake


def job(settings: Settings, state: str = "access") -> tuple[int, str]:
    """A linked installer and a job; (chat_id, external_id)."""
    chat, ext = int(uuid4().int % 10**9), f"j-{uuid4().hex[:8]}"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        installer = conn.execute(
            "INSERT INTO installation_installers (name, chat_id) VALUES (%s, %s) RETURNING id",
            (f"Ivan {ext}", chat),
        ).fetchone()
        assert installer is not None
        conn.execute(
            "INSERT INTO installation_jobs"
            " (external_id, installer_id, site, skus, appointment_at, state)"
            " VALUES (%s, %s, 'Riverside', %s, now(), %s)",
            (ext, installer[0], Jsonb(["CTL-H400"]), state),
        )
    return chat, ext


def say(chat: int, text: str, photo: str | None = None) -> None:
    uid = int(uuid4().int % 10**12)
    message: dict[str, Any] = {"message_id": 1, "chat": {"id": chat}, "text": text}
    if photo:
        message["photo"] = [{"file_id": photo}]

    async def deliver() -> None:
        await intake.receive({"update_id": uid, "message": message})
        handle: WorkflowHandleAsync[str] = await DBOS.retrieve_workflow_async(
            domain.update_workflow_id(uid)
        )
        await handle.get_result()

    dbos_async.run(deliver())


def get(ext: str) -> Any:
    return dbos_async.run(service.get_job(ext))


def test_messages_while_taken_over_are_recorded_only(dbos_runtime: Settings, bot: Bot) -> None:
    chat, ext = job(dbos_runtime)
    held = dbos_async.run(service.take_over(ext, "olga", 0))
    assert (held.taken_over_by, held.version) == ("olga", 1)
    say(chat, "done, the box is cracked", photo="f-1")
    after = get(ext)
    assert (after.state, after.version) == ("access", 1)
    assert bot.sent == []
    assert [(m.author, m.text, m.photo_description) for m in after.messages] == [
        ("installer", "done, the box is cracked", "a cracked controller")
    ]


def test_release_hands_back_to_the_agent(dbos_runtime: Settings, bot: Bot) -> None:
    chat, ext = job(dbos_runtime)
    dbos_async.run(service.take_over(ext, "olga", 0))
    released = dbos_async.run(service.release(ext, "olga", 1))
    assert (released.taken_over_by, released.version) == (None, 2)
    say(chat, "done, found the equipment")
    assert get(ext).state == "equipment_found"
    assert [m["text"] for m in bot.sent] == ["ok"]


def test_only_the_holder_sends_and_releases(dbos_runtime: Settings, bot: Bot) -> None:
    _, ext = job(dbos_runtime)
    dbos_async.run(service.take_over(ext, "olga", 0))
    with pytest.raises(service.NotHolderError):
        dbos_async.run(service.send(ext, "ivan", "hello", "k-ivan"))
    with pytest.raises(service.NotHolderError):
        dbos_async.run(service.release(ext, "ivan", 1))
    dbos_async.run(service.release(ext, "olga", 1))
    with pytest.raises(service.NotHolderError):
        dbos_async.run(service.send(ext, "olga", "hello", "k-late"))
    assert bot.sent == []
    assert get(ext).messages == []


def test_a_dispatcher_message_is_sent_once_per_key(dbos_runtime: Settings, bot: Bot) -> None:
    chat, ext = job(dbos_runtime)
    dbos_async.run(service.take_over(ext, "olga", 0))
    for _ in range(2):
        dbos_async.run(service.send(ext, "olga", "I am calling the site now.", "k-1"))
    assert bot.sent == [{"chat_id": chat, "text": "I am calling the site now."}]
    assert [(m.author, m.text) for m in get(ext).messages] == [
        ("dispatcher:olga", "I am calling the site now.")
    ]


def test_advance_moves_one_step_without_a_photo(dbos_runtime: Settings, bot: Bot) -> None:
    _, ext = job(dbos_runtime, state="equipment_found")
    moved = dbos_async.run(service.advance(ext, "olga", 0))
    assert (moved.state, moved.version) == ("installed", 1)
    with pytest.raises(service.StaleVersionError):
        dbos_async.run(service.advance(ext, "olga", 0))


def test_advancing_to_arrived_settles_the_arrival_deadline(
    dbos_runtime: Settings, bot: Bot
) -> None:
    _, ext = job(dbos_runtime, state="assigned")
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO installation_deadlines (job_id, kind, due_at)"
            " SELECT id, 'arrival', %s FROM installation_jobs WHERE external_id = %s",
            (datetime.now(UTC) + timedelta(hours=1), ext),
        )
    dbos_async.run(service.advance(ext, "olga", 0))
    with psycopg.connect(dbos_runtime.database_url) as conn:
        row = conn.execute("SELECT settled_at IS NOT NULL FROM installation_deadlines").fetchone()
        event = conn.execute(
            "SELECT kind, actor, data FROM installation_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == (True,)
    assert event == (
        "manual_advance",
        "dispatcher:olga",
        {"from": "assigned", "to": "arrived"},
    )


def test_a_takeover_at_a_stale_version_is_refused(dbos_runtime: Settings, bot: Bot) -> None:
    _, ext = job(dbos_runtime)
    dbos_async.run(service.take_over(ext, "olga", 0))
    with pytest.raises(service.StaleVersionError):
        dbos_async.run(service.take_over(ext, "ivan", 0))


def test_deadlines_keep_running_while_held(dbos_runtime: Settings, bot: Bot) -> None:
    chat, ext = job(dbos_runtime, state="assigned")
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO installation_deadlines (job_id, kind, due_at)"
            " SELECT id, 'arrival', now() - interval '5 minutes' FROM installation_jobs"
            " WHERE external_id = %s",
            (ext,),
        )
    dbos_async.run(service.take_over(ext, "olga", 0))
    dbos_async.run(workflow.check_once())
    assert [m["chat_id"] for m in bot.sent] == [chat]


@pytest.fixture
def client(bot: Bot) -> Iterator[TestClient]:
    app = FastAPI(lifespan=dbos_async.lifespan)
    app.include_router(api.router)
    with TestClient(app) as c:
        yield c


def test_the_api_takes_over_sends_and_releases(
    dbos_runtime: Settings, client: TestClient, bot: Bot
) -> None:
    chat, ext = job(dbos_runtime)
    base = f"/api/v1/installation/jobs/{ext}"
    held = client.post(f"{base}/takeover", json={"operator": "olga", "version": 0})
    assert held.json()["taken_over_by"] == "olga"
    body = {"operator": "olga", "text": "On my way."}
    assert client.post(f"{base}/messages", json=body).status_code == 422, "needs a key"
    sent = client.post(f"{base}/messages", json=body, headers={"Idempotency-Key": "m-1"})
    assert sent.status_code == 200
    other = client.post(
        f"{base}/messages",
        json={"operator": "ivan", "text": "hi"},
        headers={"Idempotency-Key": "m-2"},
    )
    assert other.status_code == 409
    assert (
        client.post(f"{base}/release", json={"operator": "olga", "version": 1}).status_code == 200
    )
    assert (
        client.post(f"{base}/advance", json={"operator": "olga", "version": 2}).status_code == 200
    )
    assert (
        client.post(f"{base}/advance", json={"operator": "olga", "version": 2}).status_code == 409
    )
    assert [m["chat_id"] for m in bot.sent] == [chat]


def test_a_held_message_and_a_quiet_turn_refresh_the_page(dbos_runtime: Settings, bot: Bot) -> None:
    import asyncio  # noqa: PLC0415

    from mappd import events  # noqa: PLC0415

    chat, ext = job(dbos_runtime)
    other_chat, other = job(dbos_runtime)
    dbos_async.run(service.take_over(ext, "olga", 0))

    async def scenario() -> list[tuple[str, str]]:
        got: list[tuple[str, str]] = []

        async def collect() -> None:
            async for e in events.BROKER.subscribe("installation"):
                got.append((e.case_id, e.type))

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await asyncio.to_thread(say, chat, "anyone there?")
        await asyncio.to_thread(say, other_chat, "where is the key?")
        await asyncio.sleep(0.1)
        task.cancel()
        return got

    got = dbos_async.run(scenario())
    assert (ext, "message") in got, "the held message refreshes the holder's page"
    assert (other, "message") in got, "a turn that moves nothing still refreshes the page"
