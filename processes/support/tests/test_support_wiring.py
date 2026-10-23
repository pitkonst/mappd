from pathlib import Path

import pytest
from dbos._registrations import get_dbos_func_name
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_ai.models.openai import OpenAIChatModel

import processes
from mappd.db.migrate import upgrade
from mappd.process import Process
from mappd.settings import Settings
from processes.support import api, pages, process, service, workflow
from processes.support.agents import classifier, drafter

PROCESSES = Path(__file__).parents[2]


def support_app() -> FastAPI:
    app = FastAPI()
    app.include_router(api.router)
    return app


def test_support_process_is_explicitly_registered() -> None:
    assert isinstance(process, Process)
    assert not hasattr(process, "root")
    assert not hasattr(process, "workflows")
    assert not hasattr(process, "agents")
    assert not hasattr(process, "entry")
    assert process in processes.PROCESSES
    assert process.name == "support"
    assert process.router is api.router
    assert process.pages is pages.router
    assert process.ingest is service.ingest
    assert process.migrations_dir is not None
    assert process.migrations_dir.is_dir()
    assert process.evals is not None
    assert process.synth is not None
    assert process.seed is not None
    assert set(process.mocks) == {"llm", "webhook"}


def test_agents_live_in_their_own_folders_and_use_gateway_models() -> None:
    support_dir = Path(__file__).parents[1]
    assert (support_dir / "agents" / "classifier" / "prompt.md").is_file()
    assert (support_dir / "agents" / "drafter" / "prompt.md").is_file()
    models = {}
    for agent in (classifier, drafter):
        assert isinstance(agent.model, OpenAIChatModel)
        models[agent.name] = agent.model.model_name
    assert models == {"support.classifier": "fast", "support.drafter": "smart"}


def test_dbos_names_are_namespaced() -> None:
    functions = (workflow.process_ticket, workflow.approve_and_send, workflow.load_ticket)
    assert {get_dbos_func_name(f) for f in functions} == {
        "support.process_ticket",
        "support.approve_and_send",
        "support.load_ticket",
    }


def test_api_routes() -> None:
    # FastAPI 0.143 includes routers lazily; the OpenAPI schema lists every path.
    paths = set(support_app().openapi()["paths"])
    assert {
        "/api/v1/support/tickets",
        "/api/v1/support/tickets/{external_id}",
        "/api/v1/support/tickets/{external_id}/approve",
        "/api/v1/support/schema",
        "/api/v1/support/tickets/{external_id}/escalate",
        "/api/v1/support/tickets/{external_id}/resolve",
    } <= paths


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/support/tickets/nope"),
        ("post", "/api/v1/support/tickets/nope/approve"),
    ],
)
def test_unknown_ticket_is_404(
    fresh_database_url: str, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    upgrade(Settings(database_url=fresh_database_url, processes_dir=PROCESSES))
    monkeypatch.setenv("MAPPD_DATABASE_URL", fresh_database_url)
    body = {"version": 1, "operator": "olga", "reply": "hi"}
    response = TestClient(support_app()).request(
        method, path, json=body if method == "post" else None
    )
    assert response.status_code == 404
