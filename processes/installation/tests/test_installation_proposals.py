"""A dispatcher confirms or rejects the resolver's proposals; a confirmed one runs once."""

import asyncio
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

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
from processes.installation import api, service, telegram, workflow
from processes.installation.db import METADATA
from processes.installation.mocks.systems import Facts, Slot, Systems


class Fakes:
    def __init__(self) -> None:
        self.systems = Systems(Facts(stock={"CTL-H400": 3}))
        self.sent: list[dict[str, Any]] = []


@pytest.fixture
def fakes(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> Fakes:
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
    out = Fakes()

    class Client(TelegramClient):
        def __init__(self) -> None:
            pass

        def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
            out.sent.append({"chat_id": chat_id, "text": text})
            return {"message_id": len(out.sent)}

    async def call(url: str, tool: str, arguments: dict[str, Any], **_: Any) -> dict[str, Any]:
        result: dict[str, Any] = getattr(out.systems, tool)(**arguments)
        return result

    monkeypatch.setattr(telegram, "client", Client)
    monkeypatch.setattr(workflow, "mcp_call", call)
    return out


def blocked_job(settings: Settings, ext: str) -> str:
    """A blocked job with one pending reserve_parts proposal; the proposal id."""
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        installer = conn.execute(
            "INSERT INTO installation_installers (name, chat_id) VALUES (%s, %s) RETURNING id",
            (f"Ivan {ext}", int(uuid4().int % 10**9)),
        ).fetchone()
        assert installer is not None
        job = conn.execute(
            "INSERT INTO installation_jobs (external_id, installer_id, site, skus,"
            " appointment_at, state, blocked_from, blocked_reason, version)"
            " VALUES (%s, %s, 'Riverside', %s, now(), 'blocked', 'access', 'missing_parts', 2)"
            " RETURNING id",
            (ext, installer[0], Jsonb(["CTL-H400"])),
        ).fetchone()
        assert job is not None
        proposal = conn.execute(
            "INSERT INTO installation_proposals (job_id, tool, arguments, reason)"
            " VALUES (%s, 'reserve_parts', %s, 'in stock') RETURNING id",
            (job[0], Jsonb({"job": ext, "skus": ["CTL-H400"]})),
        ).fetchone()
        assert proposal is not None
    return str(proposal[0])


def reservations(fakes: Fakes) -> list[dict[str, Any]]:
    return [args for name, args in fakes.systems.calls if name == "reserve_parts"]


def state(settings: Settings, ext: str) -> str:
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            "SELECT state FROM installation_jobs WHERE external_id = %s", (ext,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_confirm_runs_the_tool_once_and_tells_the_installer(
    dbos_runtime: Settings, fakes: Fakes
) -> None:
    pid = blocked_job(dbos_runtime, "p-1")
    got = dbos_async.run(service.confirm("p-1", pid, "olga"))
    assert (got.status, got.decided_by) == ("done", "olga")
    assert got.result is not None
    assert got.result["ok"] is True
    assert reservations(fakes) == [
        {"job": "p-1", "skus": ["CTL-H400"], "key": f"installation.run_tool:{pid}"}
    ]
    assert len(fakes.sent) == 1
    assert "reserved" in fakes.sent[0]["text"].lower()
    job = dbos_async.run(service.get_job("p-1"))
    assert [(m.author, m.text) for m in job.messages] == [("bot", fakes.sent[0]["text"])]
    assert state(dbos_runtime, "p-1") == "blocked", "confirming does not unblock"


def test_confirm_twice_reserves_once(dbos_runtime: Settings, fakes: Fakes) -> None:
    pid = blocked_job(dbos_runtime, "p-2")

    async def twice() -> list[str]:
        both = await asyncio.gather(
            service.confirm("p-2", pid, "olga"), service.confirm("p-2", pid, "ivan")
        )
        return [p.status for p in both]

    assert dbos_async.run(twice()) == ["done", "done"]
    assert len(reservations(fakes)) == 1
    assert len(fakes.sent) == 1


def test_confirm_resumes_after_a_crash_without_a_second_call(
    dbos_runtime: Settings, fakes: Fakes
) -> None:
    # A crash after the warehouse answered but before DBOS checkpointed the step: the step
    # runs again on recovery, and the outbox answers it from the stored result.
    pid = blocked_job(dbos_runtime, "p-3")
    first = dbos_async.run(
        workflow.run_tool(pid, "reserve_parts", {"job": "p-3", "skus": ["CTL-H400"]})
    )
    got = dbos_async.run(service.confirm("p-3", pid, "olga"))
    assert got.status == "done"
    assert got.result == first
    assert len(reservations(fakes)) == 1


def test_reject_calls_nothing(dbos_runtime: Settings, fakes: Fakes) -> None:
    pid = blocked_job(dbos_runtime, "p-4")
    got = dbos_async.run(service.reject("p-4", pid, "olga", "parts are on the van"))
    assert (got.status, got.decided_by) == ("rejected", "olga")
    assert fakes.systems.calls == []
    with pytest.raises(service.StaleVersionError):
        dbos_async.run(service.confirm("p-4", pid, "olga"))


@pytest.fixture
def client(fakes: Fakes) -> Iterator[TestClient]:
    app = FastAPI(lifespan=dbos_async.lifespan)
    app.include_router(api.router)
    with TestClient(app) as c:
        yield c


def test_the_api_confirms_and_refuses_another_jobs_proposal(
    dbos_runtime: Settings, client: TestClient
) -> None:
    pid = blocked_job(dbos_runtime, "p-5")
    blocked_job(dbos_runtime, "p-6")
    base = "/api/v1/installation/jobs"
    foreign = client.post(f"{base}/p-6/proposals/{pid}/confirm", json={"operator": "olga"})
    assert foreign.status_code == 404
    ok = client.post(f"{base}/p-5/proposals/{pid}/confirm", json={"operator": "olga"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "done"
    rejected = client.post(
        f"{base}/p-5/proposals/{pid}/reject", json={"operator": "olga", "note": "late"}
    )
    assert rejected.status_code == 409
    listed = client.get(f"{base}/p-5").json()["proposals"]
    assert [(p["id"], p["status"]) for p in listed] == [(pid, "done")]


def test_proposal_arguments_are_checked_per_tool() -> None:
    from pydantic import ValidationError  # noqa: PLC0415

    from processes.installation.schemas import Proposal  # noqa: PLC0415

    ok = Proposal(tool="reserve_parts", arguments={"skus": ["CTL-H400"]}, reason="r")
    assert ok.arguments == {"skus": ["CTL-H400"]}
    for bad in (
        {"tool": "reserve_parts", "arguments": {"skus": "CTL-H400"}},
        {"tool": "reserve_parts", "arguments": {"skus": []}},
        {"tool": "book_delivery", "arguments": {"job": "j-1"}},
    ):
        with pytest.raises(ValidationError):
            Proposal.model_validate({**bad, "reason": "r"})


def test_confirm_runs_on_the_proposals_own_job(dbos_runtime: Settings, fakes: Fakes) -> None:
    pid = blocked_job(dbos_runtime, "p-7")
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE installation_proposals SET arguments = %s WHERE id = %s",
            (Jsonb({"job": "someone-else", "skus": ["CTL-H400"]}), pid),
        )
    dbos_async.run(service.confirm("p-7", pid, "olga"))
    assert [r["job"] for r in reservations(fakes)] == ["p-7"]


def test_a_tool_error_finishes_the_proposal_as_failed(
    dbos_runtime: Settings, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mappd import mcp  # noqa: PLC0415

    pid = blocked_job(dbos_runtime, "p-8")

    async def refuse(*_a: object, **_k: object) -> dict[str, Any]:
        raise mcp.McpToolError("reserve_parts: unknown sku")

    monkeypatch.setattr(workflow, "mcp_call", refuse)
    got = dbos_async.run(service.confirm("p-8", pid, "olga"))
    assert got.status == "failed"
    assert got.result == {"ok": False, "error": "reserve_parts: unknown sku"}
    assert fakes.sent == []


def test_a_booked_delivery_is_told_with_its_slot(dbos_runtime: Settings, fakes: Fakes) -> None:
    pid = blocked_job(dbos_runtime, "p-9")
    fakes.systems.facts.slots = [Slot(at="2026-10-23T09:00")]
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE installation_proposals SET tool = 'book_delivery', arguments = %s"
            " WHERE id = %s",
            (Jsonb({"slot": "2026-10-23T09:00"}), pid),
        )
    got = dbos_async.run(service.confirm("p-9", pid, "olga"))
    assert got.status == "done"
    assert "2026-10-23T09:00" in fakes.sent[0]["text"]


def test_a_confirm_whose_workflow_fails_is_an_upstream_error(
    dbos_runtime: Settings, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = blocked_job(dbos_runtime, "p-10")

    async def broken(*_a: object, **_k: object) -> dict[str, Any]:
        raise ValueError("the gateway refused the call")

    monkeypatch.setattr(workflow, "mcp_call", broken)
    base = "/api/v1/installation/jobs/p-10"
    for _ in range(2):
        r = client.post(f"{base}/proposals/{pid}/confirm", json={"operator": "olga"})
        assert r.status_code == 502, r.text
        assert "confirmed" in r.json()["detail"]
