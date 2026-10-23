import contextlib
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import uuid4

import pytest
import stranded
from dbos import DBOS, SetWorkflowID
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mappd import telemetry, workflow_stats, workflows
from mappd.web import errors, workflows_api

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mappd.settings import Settings

BASE = "/api/v1/workflows"


def make_client(settings: Settings) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    errors.install(app)
    app.include_router(workflows_api.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def close_shared() -> Iterator[None]:
    """The app's lifespan closes these; the test apps run without it."""
    yield
    workflows.close_shared()
    workflow_stats.close()


@pytest.fixture
def client(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    return make_client(dbos_runtime)


def done(wid: str | None = None) -> str:
    wid = wid or f"done-{uuid4()}"
    with SetWorkflowID(wid):
        DBOS.start_workflow(stranded.flaky_workflow).get_result()
    return wid


def url(wid: str, suffix: str = "") -> str:
    return f"{BASE}/{quote(wid, safe='')}{suffix}"


def test_list_and_filters(client: TestClient) -> None:
    prefix = f"api-{uuid4()}"
    wid = done(f"{prefix}-a")
    rows = client.get(BASE, params={"id_prefix": prefix}).json()
    assert [r["workflow_id"] for r in rows] == [wid]
    assert rows[0]["status"] == "SUCCESS"
    assert rows[0]["process"] == "test"
    params: dict[str, Any] = {"id_prefix": prefix, "status": ["ERROR", "PENDING"]}
    assert client.get(BASE, params=params).json() == []
    assert client.get(BASE, params={"id_prefix": prefix, "process": "test"}).json()
    assert client.get(BASE, params={"limit": 0}).status_code == 422


def test_list_stranded(client: TestClient, dbos_runtime: Settings) -> None:
    wid = stranded.strand(dbos_runtime, "old")
    try:
        rows = client.get(BASE, params={"status": "stranded", "limit": 500}).json()
        assert wid in {r["workflow_id"] for r in rows}
        assert all(r["stranded"] for r in rows)
    finally:
        workflows.cancel(dbos_runtime, wid)


def test_list_rejects_an_unknown_status(client: TestClient) -> None:
    assert client.get(BASE, params={"status": "FINE"}).status_code == 422


def test_stats_counts(client: TestClient) -> None:
    done()
    stats = client.get(f"{BASE}/stats", params={"window": "1h"}).json()
    assert stats["by_status"]["SUCCESS"] >= 1
    assert "test" in stats["processes"]
    assert client.get(f"{BASE}/stats", params={"window": "1y"}).status_code == 422


def test_detail_and_404(client: TestClient) -> None:
    wid = done()
    detail = client.get(url(wid)).json()
    assert detail["workflow_id"] == wid
    assert [s["name"] for s in detail["steps"]] == ["test.stranded.first", "test.flaky.step"]
    assert detail["allowed"] == ["fork"]
    assert detail["root_workflow_id"] == wid
    assert client.get(url(f"nope-{uuid4()}")).status_code == 404


def test_cancel_resume_retry_fork_status_codes(client: TestClient, dbos_runtime: Settings) -> None:
    finished, failed = done(), stranded.fail()
    assert client.post(url(finished, "/cancel")).status_code == 409
    assert client.post(url(finished, "/resume")).status_code == 409
    assert client.post(url(finished, "/retry"), json={}).status_code == 409
    assert client.post(url("nope", "/cancel")).status_code == 404
    retried = client.post(url(failed, "/retry"), json={})
    assert retried.status_code == 200
    assert retried.json()["start_step"] == 2
    assert client.post(url(failed, "/retry"), json={"to_version": "typo"}).status_code == 422
    forked = client.post(url(finished, "/fork"), json={"start_step": 2})
    assert forked.status_code == 200
    assert forked.json()["new_workflow_id"] != finished
    fresh = done()
    assert client.post(url(fresh, "/fork"), json={"start_step": 9}).status_code == 422
    assert client.post(url(fresh, "/fork"), json={}).status_code == 422
    running = stranded.strand(dbos_runtime, "old")
    try:
        assert client.post(url(running, "/fork"), json={"start_step": 1}).status_code == 409
        assert client.post(url(running, "/cancel")).status_code == 200
    finally:
        with contextlib.suppress(workflows.WorkflowStateError):
            workflows.cancel(dbos_runtime, running)


def test_recover_requires_a_version(client: TestClient, dbos_runtime: Settings) -> None:
    assert client.post(f"{BASE}/recover", json={}).status_code == 422
    assert client.post(f"{BASE}/recover", json={"to_version": "typo"}).status_code == 422
    wid = stranded.strand(dbos_runtime, "old")
    moved = client.post(f"{BASE}/recover", json={"to_version": "test", "workflow_ids": [wid]})
    assert moved.status_code == 200
    assert [m["workflow_id"] for m in moved.json()] == [wid]


def test_payloads_off_hides_input_and_output(dbos_runtime: Settings) -> None:
    off = make_client(replace(dbos_runtime, workflows_payloads=False))
    detail = off.get(url(done())).json()
    assert detail["input"] is None
    assert detail["output"] is None
    assert detail["payloads"] is False


def test_ids_with_slashes_and_quotes_round_trip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No "%" in the ID: Starlette's TestClient decodes the path twice.
    wid = done(f"ticket/<m-{uuid4().hex[:6]}@example.com> o'brien")
    assert client.get(url(wid)).json()["workflow_id"] == wid
    assert client.post(url(wid, "/fork"), json={"start_step": 1}).status_code == 200
    seen: list[str] = []

    def find(_settings: Settings, workflow_id: str) -> str:
        seen.append(workflow_id)
        return "tr-9"

    monkeypatch.setattr(telemetry, "find_trace", find)
    trace = client.get(url(wid, "/trace")).json()
    assert seen == [wid]
    assert trace["trace_id"] == "tr-9"
    assert trace["url"].endswith("selectedEvaluationId=tr-9")


def test_trace_lookup_uses_the_root_workflow(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = f"parent-{uuid4()}"
    with SetWorkflowID(parent):
        DBOS.start_workflow(stranded.parent_workflow).get_result()
    [child] = client.get(url(parent)).json()["children"]
    seen: list[str] = []
    monkeypatch.setattr(telemetry, "find_trace", lambda _s, w: seen.append(w))
    client.get(url(child, "/trace"))
    assert seen == [parent]


def test_trace_lookup_falls_back_when_mlflow_is_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, dbos_runtime: Settings
) -> None:
    monkeypatch.setattr(telemetry, "find_trace", lambda _s, _w: None)
    trace = client.get(url(done(), "/trace")).json()
    assert trace == {"trace_id": None, "url": telemetry.trace_page(dbos_runtime, None)}
    assert client.get(url("nope", "/trace")).status_code == 404


def test_system_database_down_answers_503(dbos_runtime: Settings) -> None:
    dead = replace(
        dbos_runtime,
        system_database_url="postgresql://mappd:mappd@127.0.0.1:9/nope",
        app_name=f"dead-{uuid4().hex[:6]}",
    )
    assert make_client(dead).get(BASE).status_code == 503


def test_openapi_lists_the_routes(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert {
        BASE,
        f"{BASE}/stats",
        f"{BASE}/recover",
        f"{BASE}/{{workflow_id}}",
        f"{BASE}/{{workflow_id}}/fork",
        f"{BASE}/{{workflow_id}}/trace",
    } <= set(paths)
