"""The async intake path: store once, fetch as a validated TicketOut."""

import asyncio

import psycopg
import pytest
import sqlalchemy as sa
from dbos_async import run as arun
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.testclient import TestClient

from mappd import events
from mappd.db.migrate import sqlalchemy_url
from mappd.events import Event
from mappd.settings import Settings
from mappd.web import errors
from processes.support import api, service
from processes.support.db import TICKETS
from processes.support.schemas import TicketIn, TicketOut


@pytest.fixture
def tables(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """The tickets table in the session DB; aconnect reads MAPPD_DATABASE_URL."""
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    engine = sa.create_engine(sqlalchemy_url(dbos_runtime.database_url))
    try:
        TICKETS.create(engine, checkfirst=True)
    finally:
        engine.dispose()
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM support_tickets")
    return dbos_runtime


TICKET = TicketIn(
    external_id="api-1", sender="anna@example.com", subject="NVR full", body="It beeps."
)


def test_store_is_idempotent_and_fetch_returns_a_ticket_out(tables: Settings) -> None:
    arun(service.store(TICKET))
    arun(service.store(TICKET))
    out = arun(service.get_ticket("api-1"))
    assert isinstance(out, TicketOut)
    assert (out.external_id, out.status, out.output, out.version) == ("api-1", "new", None, 0)
    with psycopg.connect(tables.database_url) as conn:
        count = conn.execute("SELECT count(*) FROM support_tickets").fetchone()
    assert count == (1,)


def test_fetch_ticket_404s_on_the_unknown(tables: Settings) -> None:
    with pytest.raises(service.NotFoundError):
        arun(service.get_ticket("nope"))


def test_get_ticket_endpoint_returns_the_row(tables: Settings) -> None:
    arun(service.store(TICKET))
    app = FastAPI()
    app.include_router(api.router)
    response = TestClient(app).get("/api/v1/support/tickets/api-1")
    assert response.status_code == 200
    body = response.json()
    assert body["external_id"] == "api-1"
    assert body["output"] is None


def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    errors.install(app)  # as the app does: a 409 renders flat
    return TestClient(app)


def test_there_is_no_telegram_intake() -> None:
    """Support replies by email only, so it takes no Telegram tickets."""
    update = {"update_id": 42, "message": {"chat": {"id": 7}, "text": "the valve rattles"}}
    assert client().post("/api/v1/support/inbound/telegram", json=update).status_code == 404


def test_list_endpoint_pages(tables: Settings) -> None:
    arun(service.store(TICKET))
    body = client().get("/api/v1/support/tickets", params={"status": "new"}).json()
    assert [t["external_id"] for t in body["items"]] == ["api-1"]
    assert body["next"] is None
    assert client().get("/api/v1/support/tickets", params={"before": "x"}).status_code == 422


def test_schema_endpoint_serves_output_and_decision() -> None:
    body = client().get("/api/v1/support/schema").json()
    assert body["output"]["title"] == "SupportOutput"
    assert set(body["decision"]["properties"]) == {"version", "operator", "reply", "topic"}


def test_approve_maps_conflict_and_mismatch(
    tables: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    arun(service.store(TICKET))

    async def stale(*_: object, **__: object) -> None:
        raise service.StaleVersionError(4, "sent")

    monkeypatch.setattr(service, "approve", stale)
    body = {"version": 1, "operator": "olga", "reply": "x"}
    r = client().post("/api/v1/support/tickets/api-1/approve", json=body)
    assert r.status_code == 409
    assert r.json() == {
        "detail": "ticket changed or already decided",
        "current_version": 4,
        "status": "sent",
    }

    async def mismatch(*_: object, **__: object) -> None:
        raise service.IdempotencyMismatchError("k reused")

    monkeypatch.setattr(service, "approve", mismatch)
    r = client().post(
        "/api/v1/support/tickets/api-1/approve", json=body, headers={"Idempotency-Key": "k"}
    )
    assert r.status_code == 422


def test_events_endpoint_streams_support_events() -> None:
    """The stream yields support events only, each with its stable id.

    Starlette's TestClient waits for the app to finish, so it cannot read an endless stream:
    drive the endpoint's generator directly and check the route is an event stream.
    """

    async def first() -> ServerSentEvent:
        stream = aiter(api.stream())
        pending = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        events.BROKER.publish(Event.of("rfq", "r-1", "created", 0))
        events.BROKER.publish(Event.of("support", "m-1", "created", 0))
        return await asyncio.wait_for(pending, 2)

    sse = asyncio.run(first())
    assert sse.id == "support:m-1:created:0"
    assert sse.data["case_id"] == "m-1"
    [route] = [r for r in api.router.routes if getattr(r, "path", "") == "/api/v1/support/events"]
    assert isinstance(route, APIRoute)
    assert route.response_class is EventSourceResponse


def test_a_failed_send_is_502_naming_the_workflow(
    tables: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    arun(service.store(TICKET))

    async def failed(*_: object, **__: object) -> None:
        raise service.SendFailedError("support.approve:api-1:v1")

    monkeypatch.setattr(service, "approve", failed)
    body = {"version": 1, "operator": "olga", "reply": "x"}
    r = client().post("/api/v1/support/tickets/api-1/approve", json=body)
    assert r.status_code == 502
    assert "support.approve:api-1:v1" in r.json()["detail"]


def test_escalate_and_resolve_endpoints_map_errors(
    tables: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    arun(service.store(TICKET))

    async def stale(*_: object, **__: object) -> None:
        raise service.StaleVersionError(2, "resolved")

    for path, call, body in (
        ("escalate", "escalate", {"version": 0, "operator": "olga", "escalate_to": "sales"}),
        ("resolve", "resolve", {"version": 0, "operator": "olga"}),
    ):
        monkeypatch.setattr(service, call, stale)
        r = client().post(f"/api/v1/support/tickets/api-1/{path}", json=body)
        assert r.status_code == 409
        assert r.json()["status"] == "resolved"


def test_an_unknown_status_filter_is_refused() -> None:
    r = client().get("/api/v1/support/tickets", params={"status": "nope"})
    assert r.status_code == 422
    assert "'all'" in r.json()["detail"][0]["msg"], "the message names every accepted value"
