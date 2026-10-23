from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mappd.mocks.llm_router import build
from mappd.process import MockSpec


def apps() -> tuple[FastAPI, FastAPI]:
    support, rfq = FastAPI(), FastAPI()

    @support.post("/v1/chat/completions")
    async def s(body: dict[str, Any]) -> dict[str, str]:
        return {"who": "support"}

    @support.post("/v1/embeddings")
    async def e(body: dict[str, Any]) -> dict[str, str]:
        return {"who": "support-embed"}

    @support.get("/calls")
    async def sc() -> dict[str, str]:
        return {"who": "support-calls"}

    @rfq.post("/v1/chat/completions")
    async def r(body: dict[str, Any]) -> dict[str, str]:
        return {"who": "rfq"}

    @rfq.get("/rfq/calls")
    async def rc() -> dict[str, str]:
        return {"who": "rfq-calls"}

    return support, rfq


def claims(body: dict[str, Any]) -> bool:
    return "RFQ" in str(body.get("messages", "")) or str(body.get("__path__", "")).startswith(
        "/rfq/"
    )


def test_router_dispatches_by_claims_with_a_fallback() -> None:
    support, rfq = apps()
    c = TestClient(build([MockSpec(app=support), MockSpec(app=rfq, claims=claims)]))
    assert c.post(
        "/v1/chat/completions", json={"messages": [{"content": "RFQ please"}]}
    ).json() == {"who": "rfq"}
    assert c.post("/v1/chat/completions", json={"messages": [{"content": "ticket"}]}).json() == {
        "who": "support"
    }
    assert c.post("/v1/embeddings", json={"input": "x"}).json() == {"who": "support-embed"}
    assert c.get("/calls").json() == {"who": "support-calls"}
    assert c.get("/rfq/calls").json() == {"who": "rfq-calls"}


def test_a_single_mock_is_served_as_is() -> None:
    support, _ = apps()
    c = TestClient(build([MockSpec(app=support)]))
    assert c.post("/v1/chat/completions", json={}).json() == {"who": "support"}
