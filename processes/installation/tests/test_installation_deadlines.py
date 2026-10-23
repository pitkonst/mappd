"""Deadlines are rows; a per-minute check acts on each overdue one exactly once."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa
from psycopg.types.json import Jsonb

from mappd import events
from mappd.channels.telegram import TelegramClient
from mappd.db.migrate import sqlalchemy_url
from mappd.events import Event
from mappd.settings import Settings
from processes.installation import service, telegram, workflow
from processes.installation.db import METADATA


@pytest.fixture
def sent(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
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
    out: list[dict[str, object]] = []

    class Client(TelegramClient):
        def __init__(self) -> None:
            pass

        def send_message(self, chat_id: int, text: str) -> dict[str, object]:
            out.append({"chat_id": chat_id, "text": text})
            return {"message_id": len(out)}

    monkeypatch.setattr(telegram, "client", Client)
    return out


def deadline(settings: Settings, kind: str, due: datetime) -> tuple[int, str]:
    """A linked installer, a job and one deadline; (deadline id, job external id)."""
    ext = f"j-{uuid4().hex[:8]}"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        installer = conn.execute(
            "INSERT INTO installation_installers (name, chat_id) VALUES (%s, %s) RETURNING id",
            (f"Ivan {uuid4().hex[:6]}", int(uuid4().int % 10**9)),
        ).fetchone()
        assert installer is not None
        job = conn.execute(
            "INSERT INTO installation_jobs"
            " (external_id, installer_id, site, skus, appointment_at)"
            " VALUES (%s, %s, 'Riverside', %s, %s) RETURNING id",
            (ext, installer[0], Jsonb([]), due),
        ).fetchone()
        assert job is not None
        row = conn.execute(
            "INSERT INTO installation_deadlines (job_id, kind, due_at) VALUES (%s, %s, %s)"
            " RETURNING id",
            (job[0], kind, due),
        ).fetchone()
        assert row is not None
    return int(row[0]), ext


def settled(settings: Settings, deadline_id: int) -> bool:
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            "SELECT settled_at FROM installation_deadlines WHERE id = %s", (deadline_id,)
        ).fetchone()
    assert row is not None
    return row[0] is not None


def notified(settings: Settings, deadline_id: int) -> bool:
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            "SELECT notified_at FROM installation_deadlines WHERE id = %s", (deadline_id,)
        ).fetchone()
    assert row is not None
    return row[0] is not None


PAST = datetime.now(UTC) - timedelta(minutes=5)
FUTURE = datetime.now(UTC) + timedelta(hours=1)


def test_an_overdue_arrival_reminds_once_and_emits(
    dbos_runtime: Settings, sent: list[dict[str, object]]
) -> None:
    did, ext = deadline(dbos_runtime, "arrival", PAST)

    async def scenario() -> list[Event]:
        got: list[Event] = []

        async def collect() -> None:
            async for e in events.BROKER.subscribe("installation"):
                got.append(e)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        assert await workflow.check_once() == 1
        await asyncio.sleep(0.05)
        task.cancel()
        return got

    got = dbos_async.run(scenario())
    assert notified(dbos_runtime, did)
    assert len(sent) == 1
    assert [(e.type, e.case_id, e.data["kind"]) for e in got] == [("overdue", ext, "arrival")]


def test_an_overdue_deadline_acts_once(
    dbos_runtime: Settings, sent: list[dict[str, object]]
) -> None:
    deadline(dbos_runtime, "arrival", PAST)

    async def overlapping() -> None:
        await asyncio.gather(workflow.check_once(), workflow.check_once())

    dbos_async.run(overlapping())
    assert len(sent) == 1


def test_a_future_deadline_waits(dbos_runtime: Settings, sent: list[dict[str, object]]) -> None:
    did, _ = deadline(dbos_runtime, "arrival", FUTURE)
    assert dbos_async.run(workflow.check_once()) == 0, "nothing due: an idle tick"
    assert not notified(dbos_runtime, did)
    assert sent == []


def test_a_dispatcher_deadline_sends_nothing_to_the_installer(
    dbos_runtime: Settings, sent: list[dict[str, object]]
) -> None:
    did, _ = deadline(dbos_runtime, "dispatcher_response", PAST)
    dbos_async.run(workflow.check_once())
    assert notified(dbos_runtime, did)
    assert sent == []


def test_arriving_settles_the_arrival_deadline(
    dbos_runtime: Settings, sent: list[dict[str, object]]
) -> None:
    did, ext = deadline(dbos_runtime, "arrival", FUTURE)
    with psycopg.connect(dbos_runtime.database_url) as conn:
        row = conn.execute(
            "SELECT id FROM installation_jobs WHERE external_id = %s", (ext,)
        ).fetchone()
    assert row is not None
    applied = dbos_async.run(
        workflow.apply_outcome(
            row[0],
            0,
            channel_key="x-1",
            chat_id=1,
            description=None,
            previous="assigned",
            state="arrived",
            blocked_from=None,
            blocked_reason=None,
            reply="ok",
            now=datetime.now(UTC),
        )
    )
    assert applied
    assert settled(dbos_runtime, did)


def test_a_notified_deadline_stays_overdue_until_resolved(
    dbos_runtime: Settings, sent: list[dict[str, object]]
) -> None:
    # The check acts once, but the dispatcher must keep seeing the missed arrival.
    did, ext = deadline(dbos_runtime, "arrival", PAST)
    dbos_async.run(workflow.check_once())
    assert len(sent) == 1
    assert dbos_async.run(service.get_job(ext)).overdue == ["arrival"]
    assert not settled(dbos_runtime, did), "only arriving settles it"
    dbos_async.run(workflow.check_once())
    assert len(sent) == 1, "notified once"


def test_the_reminder_is_stored_as_the_bots_message(
    dbos_runtime: Settings, sent: list[dict[str, object]]
) -> None:
    _, ext = deadline(dbos_runtime, "arrival", PAST)
    dbos_async.run(workflow.check_once())
    messages = dbos_async.run(service.get_job(ext)).messages
    assert [(m.direction, m.author) for m in messages] == [("out", "bot")]
    assert messages[0].text == workflow.REMINDER


def test_two_overdue_deadlines_of_one_job_emit_distinct_events(
    dbos_runtime: Settings, sent: list[dict[str, object]]
) -> None:
    _, ext = deadline(dbos_runtime, "arrival", PAST)
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO installation_deadlines (job_id, kind, due_at)"
            " SELECT id, 'dispatcher_response', %s FROM installation_jobs WHERE external_id = %s",
            (PAST, ext),
        )

    async def scenario() -> list[Event]:
        got: list[Event] = []

        async def collect() -> None:
            async for e in events.BROKER.subscribe("installation"):
                got.append(e)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        assert await workflow.check_once() == 2
        await asyncio.sleep(0.05)
        task.cancel()
        return got

    got = dbos_async.run(scenario())
    assert len({e.id for e in got}) == 2


def test_a_blocked_job_gets_no_arrival_reminder(
    dbos_runtime: Settings, sent: list[dict[str, object]]
) -> None:
    did, ext = deadline(dbos_runtime, "arrival", PAST)
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE installation_jobs SET state = 'blocked', blocked_from = 'assigned',"
            " blocked_reason = 'no_access' WHERE external_id = %s",
            (ext,),
        )
    dbos_async.run(workflow.check_once())
    assert notified(dbos_runtime, did)
    assert sent == []
    assert dbos_async.run(service.get_job(ext)).messages == []
