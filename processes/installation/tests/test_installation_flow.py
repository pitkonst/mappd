"""Updates are stored once, handled once, in order per job; linking and polling rules."""

import asyncio
import json
import threading
import time
from datetime import UTC, datetime
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
from processes.installation import api, domain, intake, telegram, workflow
from processes.installation.db import METADATA
from processes.installation.engine import JobView, ToolCall
from processes.installation.schemas import Interpretation, Proposal, Resolution, State


class Bot:
    """A fake Bot API: records sends, serves updates and one photo."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.endswith("/sendMessage"):
            with self.lock:
                self.sent.append(json.loads(request.content))
            return httpx2.Response(200, json={"ok": True, "result": {"message_id": 1}})
        if path.endswith("/getUpdates"):
            offset = json.loads(request.content).get("offset", 0)
            result = [u for u in self.updates if u["update_id"] >= offset]
            return httpx2.Response(200, json={"ok": True, "result": result})
        if path.endswith("/getFile"):
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
    monkeypatch.setattr(workflow, "describe_photo_text", lambda _image: "a mounted controller")

    async def interpret(
        job: JobView, nxt: State | None, text: str, photo: str | None
    ) -> Interpretation:
        lowered = text.lower()
        if "missing" in lowered:
            return Interpretation(
                intent="exception", reached_next=False, exception="missing_parts", reply="noted"
            )
        return Interpretation(
            intent="progress",
            reached_next="done" in lowered or "arrived" in lowered,
            exception=None,
            reply="ok",
        )

    monkeypatch.setattr(workflow, "interpret", interpret)

    async def resolve(
        job: JobView, reason: str, history: list[str]
    ) -> tuple[Resolution, list[ToolCall]]:
        proposal = Proposal(
            tool="reserve_parts", arguments={"job": job.external_id, "skus": job.skus}, reason="r"
        )
        call = ToolCall(tool="check_stock", arguments={"sku": job.skus[0]}, result={"on_hand": 1})
        return Resolution(summary=f"{reason} checked", reply="noted", proposals=[proposal]), [call]

    monkeypatch.setattr(workflow, "resolve", resolve)
    return fake


def job(settings: Settings, state: str = "assigned", chat: int | None = None) -> tuple[int, int]:
    """An installer (linked to `chat`, or with token 'tok-<id>') and a job; (chat_id, job_id)."""
    chat_id = chat or int(uuid4().int % 10**9)
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        installer = conn.execute(
            "INSERT INTO installation_installers (name, chat_id) VALUES (%s, %s) RETURNING id",
            (f"Ivan {uuid4().hex[:6]}", chat_id),
        ).fetchone()
        assert installer is not None
        row = conn.execute(
            "INSERT INTO installation_jobs"
            " (external_id, installer_id, site, skus, appointment_at, state)"
            " VALUES (%s, %s, 'Riverside', %s, %s, %s) RETURNING id",
            (f"j-{uuid4().hex[:8]}", installer[0], Jsonb(["CTL-H400"]), datetime.now(UTC), state),
        ).fetchone()
        assert row is not None
    return chat_id, int(row[0])


def update(update_id: int, chat: int, text: str = "", photo: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"message_id": 1, "chat": {"id": chat}, "text": text}
    if photo:
        message["photo"] = [{"file_id": photo}]
    return {"update_id": update_id, "message": message}


def state_of(settings: Settings, job_id: int) -> str:
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            "SELECT state FROM installation_jobs WHERE id = %s", (job_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def settle(update_ids: list[int]) -> None:
    async def wait() -> None:
        for uid in update_ids:
            handle: WorkflowHandleAsync[str] = await DBOS.retrieve_workflow_async(
                domain.update_workflow_id(uid)
            )
            await handle.get_result()

    dbos_async.run(wait())


def uid() -> int:
    return int(uuid4().int % 10**12)


def test_a_duplicate_update_is_processed_once(dbos_runtime: Settings, bot: Bot) -> None:
    chat, job_id = job(dbos_runtime)
    raw = update(u := uid(), chat, "I have arrived")

    async def twice() -> None:
        await asyncio.gather(intake.receive(raw), intake.receive(raw))

    dbos_async.run(twice())
    settle([u])
    assert state_of(dbos_runtime, job_id) == "arrived"
    assert len(bot.sent) == 1
    with psycopg.connect(dbos_runtime.database_url) as conn:
        count = conn.execute(
            "SELECT count(*) FROM installation_messages WHERE channel_key = %s", (str(u),)
        ).fetchone()
    assert count == (1,)


def test_updates_of_one_job_are_applied_in_order(
    dbos_runtime: Settings, bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, job_id = job(dbos_runtime)
    seen: list[str] = []
    inner = workflow.interpret

    async def slow(job: JobView, nxt: State | None, text: str, photo: str | None) -> Interpretation:
        seen.append(f"start {text}")
        await asyncio.sleep(0.2)
        seen.append(f"end {text}")
        return await inner(job, nxt, text, photo)

    monkeypatch.setattr(workflow, "interpret", slow)
    first, second = uid(), uid()

    async def both() -> None:
        await intake.receive(update(first, chat, "arrived on site"))
        await intake.receive(update(second, chat, "access done"))

    dbos_async.run(both())
    settle([first, second])
    assert seen == [
        "start arrived on site",
        "end arrived on site",
        "start access done",
        "end access done",
    ]
    assert state_of(dbos_runtime, job_id) == "access"


def test_linking_rules(dbos_runtime: Settings, bot: Bot) -> None:
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO installation_installers (name, link_token) VALUES ('Olga', 'tok-1')"
        )
    stranger, other = uid() % 10**9, uid() % 10**9
    ids = [uid(), uid(), uid(), uid()]
    dbos_async.run(intake.receive(update(ids[0], stranger, "hello")))
    dbos_async.run(intake.receive(update(ids[1], stranger, "/start wrong")))
    dbos_async.run(intake.receive(update(ids[2], stranger, "/start tok-1")))
    settle(ids[:3])  # chats are separate partitions: let the first link land first
    dbos_async.run(intake.receive(update(ids[3], other, "/start tok-1")))
    settle(ids[3:])
    # Each chat is its own partition: compare per chat, not across them.
    mine = [m["text"].lower() for m in bot.sent if m["chat_id"] == stranger]
    theirs = [m["text"].lower() for m in bot.sent if m["chat_id"] == other]
    assert "open the link" in mine[0]
    assert "not valid" in mine[1]
    assert "linked" in mine[2]
    assert len(theirs) == 1
    assert "not valid" in theirs[0], "a used token links no second chat"
    with psycopg.connect(dbos_runtime.database_url) as conn:
        row = conn.execute(
            "SELECT chat_id, link_token FROM installation_installers WHERE name = 'Olga'"
        ).fetchone()
    assert row == (stranger, None)


def test_the_start_exchange_joins_the_installers_open_job(dbos_runtime: Settings, bot: Bot) -> None:
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        installer = conn.execute(
            "INSERT INTO installation_installers (name, link_token) VALUES ('Mia', 'tok-mia')"
            " RETURNING id"
        ).fetchone()
        assert installer is not None
        job_row = conn.execute(
            "INSERT INTO installation_jobs"
            " (external_id, installer_id, site, skus, appointment_at, state)"
            " VALUES (%s, %s, 'Riverside', %s, %s, 'assigned') RETURNING id",
            (f"j-{uuid4().hex[:8]}", installer[0], Jsonb(["CTL-H400"]), datetime.now(UTC)),
        ).fetchone()
        assert job_row is not None
    chat, first = uid() % 10**9, uid()
    dbos_async.run(intake.receive(update(first, chat, "/start tok-mia")))
    settle([first])
    with psycopg.connect(dbos_runtime.database_url) as conn:
        rows = conn.execute(
            "SELECT id, direction, text, reply_to FROM installation_messages"
            " WHERE job_id = %s ORDER BY id",
            (job_row[0],),
        ).fetchall()
    start, linked = rows
    assert start[1:] == ("in", "/start", None), "the token is not kept"
    assert linked[1:] == ("out", workflow.LINKED, start[0]), "the reply points at its message"


def test_installed_needs_a_photo(dbos_runtime: Settings, bot: Bot) -> None:
    chat, job_id = job(dbos_runtime, state="equipment_found")
    first, second = uid(), uid()
    dbos_async.run(intake.receive(update(first, chat, "installed, done")))
    settle([first])
    assert state_of(dbos_runtime, job_id) == "equipment_found"
    assert "photo" in bot.sent[-1]["text"].lower()
    dbos_async.run(intake.receive(update(second, chat, "installed, done", photo="f-1")))
    settle([second])
    assert state_of(dbos_runtime, job_id) == "installed"
    with psycopg.connect(dbos_runtime.database_url) as conn:
        row = conn.execute(
            "SELECT photo_description FROM installation_messages WHERE channel_key = %s",
            (str(second),),
        ).fetchone()
    assert row == ("a mounted controller",)


def test_an_exception_blocks_and_starts_the_dispatcher_clock(
    dbos_runtime: Settings, bot: Bot
) -> None:
    chat, job_id = job(dbos_runtime, state="access")
    u = uid()
    dbos_async.run(intake.receive(update(u, chat, "parts are missing")))
    settle([u])
    with psycopg.connect(dbos_runtime.database_url) as conn:
        row = conn.execute(
            "SELECT state, blocked_from, blocked_reason FROM installation_jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
        deadline = conn.execute(
            "SELECT kind FROM installation_deadlines WHERE job_id = %s", (job_id,)
        ).fetchone()
    assert row == ("blocked", "access", "missing_parts")
    assert deadline == ("dispatcher_response",)


def test_polling_rereads_after_a_crash_before_the_offset_moved(
    dbos_runtime: Settings, bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, job_id = job(dbos_runtime)
    u = uid()
    bot.updates = [update(u, chat, "arrived")]
    real = workflow.save_offset

    async def crash(*_a: object, **_k: object) -> None:
        raise RuntimeError("killed before the offset moved")

    monkeypatch.setattr(workflow, "save_offset", crash)
    with pytest.raises(RuntimeError):
        dbos_async.run(workflow.poll_once())
    monkeypatch.setattr(workflow, "save_offset", real)
    dbos_async.run(workflow.poll_once())
    settle([u])
    assert state_of(dbos_runtime, job_id) == "arrived"
    assert len(bot.sent) == 1
    with psycopg.connect(dbos_runtime.database_url) as conn:
        row = conn.execute('SELECT "offset" FROM installation_polling').fetchone()
    assert row == (u + 1,)


def test_a_poll_that_cannot_reach_telegram_is_skipped_not_failed(
    dbos_runtime: Settings, bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each 5 s tick during an outage left a failed workflow: 1,763 of them in an afternoon.
    job(dbos_runtime)

    async def unreachable(offset: int) -> list[dict[str, object]]:
        raise httpx2.ConnectError("[Errno 54] Connection reset by peer")

    monkeypatch.setattr(workflow, "fetch_updates", unreachable)
    assert dbos_async.run(workflow.poll_once()) == 0
    with psycopg.connect(dbos_runtime.database_url) as conn:
        assert conn.execute('SELECT "offset" FROM installation_polling').fetchone() is None


def test_each_bot_keeps_its_own_polling_offset(
    dbos_runtime: Settings, bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The mock left the offset at 1,792,776,112; the real bot's updates were all below it.
    monkeypatch.setenv("MAPPD_INSTALLATION_TELEGRAM_TOKEN", "111:mock-token")
    dbos_async.run(workflow.save_offset(1_792_776_112))
    assert dbos_async.run(workflow.read_offset()) == 1_792_776_112
    monkeypatch.setenv("MAPPD_INSTALLATION_TELEGRAM_TOKEN", "222:real-token")
    assert dbos_async.run(workflow.read_offset()) == 0
    with psycopg.connect(dbos_runtime.database_url) as conn:
        keys = [r[0] for r in conn.execute("SELECT bot FROM installation_polling")]
    assert keys == ["111"]  # the bot's id, never the secret part of its token


def test_webhook_checks_the_secret(
    dbos_runtime: Settings, bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAPPD_INSTALLATION_TELEGRAM_SECRET", "s3")
    chat, _ = job(dbos_runtime)
    app = FastAPI(lifespan=dbos_async.lifespan)
    app.include_router(api.router)
    with TestClient(app) as c:
        path = "/api/v1/installation/inbound/telegram"
        assert c.post(path, json=update(uid(), chat, "hi")).status_code == 401
        headers = {"X-Telegram-Bot-Api-Secret-Token": "s3"}
        assert c.post(path, json=update(uid(), chat, "hi"), headers=headers).status_code == 200
    time.sleep(0)


def test_a_message_while_blocked_keeps_the_block_and_the_unblock_works(
    dbos_runtime: Settings, bot: Bot
) -> None:
    chat, job_id = job(dbos_runtime, state="access")
    first, second = uid(), uid()
    dbos_async.run(intake.receive(update(first, chat, "parts are missing")))
    settle([first])
    dbos_async.run(intake.receive(update(second, chat, "any news?")))
    settle([second])
    with psycopg.connect(dbos_runtime.database_url) as conn:
        row = conn.execute(
            "SELECT state, blocked_from, blocked_reason, version, external_id"
            " FROM installation_jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
    assert row is not None
    assert row[:4] == ("blocked", "access", "missing_parts", 1), "a reply alone moves nothing"
    result = dbos_async.run(workflow.unblock_job(row[4], 1, "olga"))
    assert result == "access"
    assert state_of(dbos_runtime, job_id) == "access"


def test_every_update_of_a_chat_shares_one_partition(dbos_runtime: Settings, bot: Bot) -> None:
    # Linking mid-stream must not split a conversation across partitions.
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO installation_installers (name, link_token) VALUES ('Petra', 'tok-p')"
        )
    chat = uid() % 10**9
    first, second = uid(), uid()
    dbos_async.run(intake.receive(update(first, chat, "/start tok-p")))
    settle([first])
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        installer = conn.execute(
            "SELECT id FROM installation_installers WHERE name = 'Petra'"
        ).fetchone()
        assert installer is not None
        conn.execute(
            "INSERT INTO installation_jobs (external_id, installer_id, site, skus, appointment_at)"
            " VALUES (%s, %s, 'Riverside', %s, now())",
            (f"j-{uuid4().hex[:8]}", installer[0], Jsonb(["CTL-H400"])),
        )
    dbos_async.run(intake.receive(update(second, chat, "I have arrived")))
    settle([second])
    with psycopg.connect(dbos_runtime.system_database_url) as conn:
        keys = conn.execute(
            "SELECT queue_partition_key FROM dbos.workflow_status WHERE workflow_uuid = ANY(%s)",
            ([domain.update_workflow_id(first), domain.update_workflow_id(second)],),
        ).fetchall()
    assert {k[0] for k in keys} == {f"chat:{chat}"}


def test_a_version_moved_meanwhile_reapplies_the_message(
    dbos_runtime: Settings, bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, job_id = job(dbos_runtime)
    inner = workflow.interpret
    calls: list[str] = []

    async def racing(
        job: JobView, nxt: State | None, text: str, photo: str | None
    ) -> Interpretation:
        calls.append(job.state)
        if len(calls) == 1:  # a dispatcher's change lands while the agent thinks
            with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
                conn.execute(
                    "UPDATE installation_jobs SET version = version + 1 WHERE id = %s", (job_id,)
                )
        return await inner(job, nxt, text, photo)

    monkeypatch.setattr(workflow, "interpret", racing)
    u = uid()
    dbos_async.run(intake.receive(update(u, chat, "I have arrived")))
    settle([u])
    assert len(calls) == 2
    assert state_of(dbos_runtime, job_id) == "arrived"
    assert len(bot.sent) == 1


def test_a_block_stores_the_resolution_proposals_and_events(
    dbos_runtime: Settings, bot: Bot
) -> None:
    chat, job_id = job(dbos_runtime, state="access")
    u = uid()
    dbos_async.run(intake.receive(update(u, chat, "parts are missing")))
    settle([u])
    with psycopg.connect(dbos_runtime.database_url) as conn:
        proposals = conn.execute(
            "SELECT tool, arguments, status FROM installation_proposals WHERE job_id = %s",
            (job_id,),
        ).fetchall()
        events = conn.execute(
            "SELECT kind, actor, data FROM installation_events WHERE job_id = %s ORDER BY id",
            (job_id,),
        ).fetchall()
    assert [(p[0], p[1]["skus"], p[2]) for p in proposals] == [
        ("reserve_parts", ["CTL-H400"], "pending")
    ]
    assert [(e[0], e[1]) for e in events] == [
        ("blocked", "agent"),
        ("resolution", "agent"),
        ("tool_call", "agent"),
        ("proposal", "agent"),
    ]
    assert events[0][2] == {"from": "access", "to": "blocked", "reason": "missing_parts"}
    assert events[1][2] == {"summary": "missing_parts checked"}
    assert bot.sent[-1]["text"] == "noted"


def test_a_step_forward_records_a_transition(dbos_runtime: Settings, bot: Bot) -> None:
    chat, job_id = job(dbos_runtime)
    u = uid()
    dbos_async.run(intake.receive(update(u, chat, "I have arrived")))
    settle([u])
    with psycopg.connect(dbos_runtime.database_url) as conn:
        events = conn.execute(
            "SELECT kind, data FROM installation_events WHERE job_id = %s", (job_id,)
        ).fetchall()
    assert events == [("transition", {"from": "assigned", "to": "arrived", "reason": None})]
    with psycopg.connect(dbos_runtime.database_url) as conn:
        inbound = conn.execute(
            "SELECT id FROM installation_messages WHERE channel_key = %s", (str(u),)
        ).fetchone()
        links = conn.execute(
            "SELECT (SELECT message_id FROM installation_events WHERE job_id = %s),"
            " (SELECT reply_to FROM installation_messages WHERE job_id = %s AND author = 'bot')",
            (job_id, job_id),
        ).fetchone()
    assert inbound is not None
    assert links == (inbound[0], inbound[0]), "the turn's event and reply point at the message"


def test_the_loaded_history_leaves_out_the_message_being_handled(
    dbos_runtime: Settings, bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, _ = job(dbos_runtime, state="access")
    seen: list[list[str]] = []
    inner = workflow.resolve

    async def resolve(
        job: JobView, reason: str, history: list[str]
    ) -> tuple[Resolution, list[ToolCall]]:
        seen.append(history)
        return await inner(job, reason, history)

    monkeypatch.setattr(workflow, "resolve", resolve)
    first, second = uid(), uid()
    dbos_async.run(intake.receive(update(first, chat, "hello there")))
    settle([first])
    dbos_async.run(intake.receive(update(second, chat, "parts are missing")))
    settle([second])
    assert seen == [["installer: hello there", "bot: ok", "installer: parts are missing"]]


def test_the_history_stops_before_the_message_being_handled(
    dbos_runtime: Settings, bot: Bot
) -> None:
    chat, job_id = job(dbos_runtime, state="access")
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        for key, text in (("h-1", "earlier"), ("h-2", "the box is missing"), ("h-3", "what now?")):
            conn.execute(
                "INSERT INTO installation_messages (job_id, chat_id, direction, channel_key, text)"
                " VALUES (%s, %s, 'in', %s, %s)",
                (job_id, chat, key, text),
            )
    record = dbos_async.run(workflow.load_job(chat, "h-2"))
    assert record is not None
    assert record.history == ["installer: earlier"]


def test_the_link_confirmation_says_what_to_send() -> None:
    # "Send updates about your job here" left a real installer asking "What updates?".
    for step in ("on site", "access", "equipment", "installed", "tested", "handed over"):
        assert step in workflow.LINKED
    assert "photo" in workflow.LINKED
