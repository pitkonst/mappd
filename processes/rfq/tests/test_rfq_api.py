"""RFQ's HTTP API: routes, schema, and error mapping."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import processes
from processes.rfq import api, process, service


def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_routes_are_under_api_v1_rfq() -> None:
    paths = set(client().app.openapi()["paths"])  # type: ignore[attr-defined]
    assert {
        "/api/v1/rfq/requests",
        "/api/v1/rfq/requests/{external_id}",
        "/api/v1/rfq/requests/{external_id}/approve",
        "/api/v1/rfq/requests/{external_id}/escalate",
        "/api/v1/rfq/requests/{external_id}/resolve",
        "/api/v1/rfq/schema",
        "/api/v1/rfq/events",
    } <= paths


def test_schema_serves_output_and_decision() -> None:
    body = client().get("/api/v1/rfq/schema").json()
    assert body["output"]["title"] == "RfqOutput"
    assert "lines" in body["decision"]["properties"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (service.NotFoundError("no RFQ"), 404),
        (service.StaleVersionError(3, "sent"), 409),
        (service.IdempotencyMismatchError("k"), 422),
        (service.InvalidLinesError("not in the catalog: NOPE-1"), 422),
        (service.SendFailedError("rfq.approve:x:v1"), 502),
    ],
)
def test_approve_maps_service_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: int
) -> None:
    async def fail(*_: object, **__: object) -> None:
        raise error

    monkeypatch.setattr(service, "approve", fail)
    r = client().post(
        "/api/v1/rfq/requests/x/approve", json={"version": 0, "operator": "olga", "reply": "x"}
    )
    assert r.status_code == code


def test_the_process_is_registered_with_its_policy() -> None:
    assert process in processes.PROCESSES
    assert process.categories == ("quote", "clarify")
    assert process.policy().level("quote") == "review"
