from typing import TYPE_CHECKING, Any
from uuid import uuid4

import dbos_async
import psycopg
import pytest
from dbos import DBOS, SetWorkflowID, WorkflowHandleAsync
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mappd.actions import OutcomeUnknownError, action
from mappd.web import api

if TYPE_CHECKING:
    from mappd.settings import Settings


CALLED: list[str] = []


@action("test.settle_send", on_unknown="escalate")
def settle_send(idempotency_key: str) -> dict[str, Any]:
    CALLED.append(idempotency_key)
    return {"sent": True}


@DBOS.workflow()
async def settle_case(key: str) -> dict[str, Any]:
    return await settle_send(key)


@pytest.fixture
def client(migrated: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MAPPD_DATABASE_URL", migrated.database_url)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def unknown(settings: Settings) -> str:
    key = f"support.send_email:<m-{uuid4().hex[:6]}@example.com>/x"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO actions (kind, idempotency_key, payload, status)"
            " VALUES ('support.send_email', %s, '{}', 'unknown')",
            (key,),
        )
    return key


def test_list_and_resolve(client: TestClient, migrated: Settings) -> None:
    key = unknown(migrated)
    rows = client.get("/api/v1/actions", params={"status": "unknown"}).json()
    assert key in {r["idempotency_key"] for r in rows}
    body = {"took_effect": True, "operator": "olga", "result": {"id": 7}}
    r = client.post(f"/api/v1/actions/{key}/resolve", json=body)
    assert r.status_code == 200
    assert r.json()["action"]["settled_by"] == "olga"
    assert r.json()["action"]["status"] == "done"
    again = client.post(f"/api/v1/actions/{key}/resolve", json=body)
    assert again.status_code == 404
    nameless = client.post(f"/api/v1/actions/{key}/resolve", json={"took_effect": True})
    assert nameless.status_code == 422, "the operator is required"


def test_unknown_status_is_422(client: TestClient) -> None:
    assert client.get("/api/v1/actions", params={"status": "stuck"}).status_code == 422


@pytest.mark.parametrize(("took_effect", "sends"), [(True, 0), (False, 1)])
def test_settling_retries_the_failed_workflow_so_the_case_finishes(
    dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch, took_effect: bool, sends: int
) -> None:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    CALLED.clear()
    key, wid = f"test-{uuid4()}", f"settle-{uuid4()}"
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(  # an attempt of this very workflow stopped mid-call: outcome unknown
            "INSERT INTO actions (kind, idempotency_key, payload, status, claimed_by, claimed_at)"
            " VALUES ('test.settle_send', %s, '{}', 'sending', %s, now())",
            (key, wid),
        )
    with SetWorkflowID(wid), pytest.raises(OutcomeUnknownError):
        dbos_async.run(settle_case(key))
    resolution = api.Resolution(took_effect=took_effect, operator="olga")

    async def settle() -> api.Settled:
        settled = await api.resolve(dbos_runtime, key, resolution)
        assert settled.retried is not None, "the ERROR workflow is retried"
        handle: WorkflowHandleAsync[Any] = await DBOS.retrieve_workflow_async(settled.retried)
        await handle.get_result()
        return settled

    settled = dbos_async.run(settle())
    assert settled.action.settled_by == "olga"
    assert len(CALLED) == sends, "took effect: never sent again; not sent: sent once"
