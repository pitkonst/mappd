import asyncio
import json as _json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mappd.events import sign
from processes.support.mocks import mock_llm, mock_webhook
from processes.support.mocks.mock_llm import check_json, synth_text
from processes.support.schemas import EMBED_DIM

llm = TestClient(mock_llm.app)


def tool_call(response: dict[str, object]) -> dict[str, str]:
    choices = response["choices"]
    assert isinstance(choices, list)
    function: dict[str, str] = choices[0]["message"]["tool_calls"][0]["function"]
    return function


def tool_args(response: dict[str, object]) -> str:
    return tool_call(response)["arguments"]


def classify(text: str) -> str:
    llm.post("/calls/reset")
    body = {"model": "fast", "messages": [{"role": "user", "content": text}], "tools": []}
    return tool_args(llm.post("/v1/chat/completions", json=body).json())


def test_classifier_routes_refunds_to_commercial_and_faults_to_technical() -> None:
    refund = _json.loads(classify("I want a refund"))
    assert (refund["topic"], refund["outcome"], refund["escalate_to"]) == (
        "refund",
        "escalate",
        "sales",
    )
    fault = _json.loads(classify("The sensor is offline"))
    assert (fault["route"], fault["outcome"], fault["safety_risk"]) == ("technical", "reply", False)
    smoke = _json.loads(classify("Smoke from the controller, it is unsafe"))
    assert (smoke["safety_risk"], smoke["outcome"], smoke["escalate_to"]) == (
        True,
        "escalate",
        "service",
    )
    assert llm.get("/calls").json() == {"classify": 1}


def test_drafter_searches_then_answers_with_citations() -> None:
    tools = [{"type": "function", "function": {"name": "search_kb"}}]
    first = {"model": "smart", "messages": [{"role": "user", "content": "offline"}], "tools": tools}
    assert tool_call(llm.post("/v1/chat/completions", json=first).json())["name"] == "search_kb"
    second = {
        **first,
        "messages": [*first["messages"], {"role": "tool", "content": "kb-101: Sensor offline"}],
    }
    assert "kb-101" in tool_args(llm.post("/v1/chat/completions", json=second).json())


def test_drafter_cites_the_closest_article_and_quotes_its_steps() -> None:
    tools = [{"type": "function", "function": {"name": "search_kb"}}]
    found = "\n".join(
        [
            "kb-101: Sensor shows offline — Replace the battery, then re-pair the sensor.",
            "kb-103: Camera night mode — Switch night mode on in the camera settings.",
        ]
    )
    ticket = {"role": "user", "content": "My door sensor is offline in the app"}
    body = {"model": "smart", "messages": [ticket, {"role": "tool", "content": found}]}
    body["tools"] = tools
    args = _json.loads(tool_args(llm.post("/v1/chat/completions", json=body).json()))
    assert args["citations"] == ["kb-101"]
    assert "Replace the battery" in args["reply"]
    assert "cited articles" not in args["reply"]


def test_embeddings_are_deterministic_unit_vectors() -> None:
    body = {"model": "embed", "input": ["sensor offline"]}
    a = llm.post("/v1/embeddings", json=body).json()["data"][0]["embedding"]
    b = llm.post("/v1/embeddings", json=body).json()["data"][0]["embedding"]
    assert a == b
    assert len(a) == EMBED_DIM
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9


def test_mocks_declare_their_ports() -> None:
    # `mappd mock <name>` binds these without --port; compose maps the same numbers.
    assert mock_llm.PORT == 9000


FIELDS_SYSTEM = {
    "role": "system",
    "content": "Return JSON with topic, route, urgency, language, outcome.",
}


def check(text: str) -> dict[str, str]:
    result: dict[str, str] = _json.loads(
        check_json([FIELDS_SYSTEM, {"role": "user", "content": text}])
    )
    return result


def test_mock_writer_states_the_facts_in_the_language() -> None:
    user = (
        "Persona: x.\nSign as: Mira Keller.\nLanguage: German.\nTone: formal.\n"
        "Facts:\n- Fact one.\n- Fact two."
    )
    text = synth_text([{"role": "system", "content": "s"}, {"role": "user", "content": user}])
    assert text.startswith("Subject: ")
    assert "Guten Tag" in text
    assert "Fact one." in text
    assert "Fact two." in text
    assert "Mira Keller" in text


def test_mock_checker_reads_route_outcome_and_language() -> None:
    got = check("Subject: x\n\nHello, I want a refund for the camera.")
    assert (got["route"], got["outcome"], got["language"]) == ("commercial", "escalate", "en")
    got = check("Subject: x\n\nHello, my sensor shows offline, how do I get it back?")
    assert (got["route"], got["outcome"]) == ("technical", "clarify")
    got = check(
        "Subject: x\n\nGuten Tag, how do I update the firmware on my controller? (order ORD-12345)"
    )
    assert (got["route"], got["outcome"], got["language"]) == ("technical", "reply", "de")


def test_mock_judge_passes_a_reply_and_fails_an_empty_one() -> None:
    def ask(reply: str) -> dict[str, object]:
        messages = [
            {"role": "system", "content": "rubric"},
            {"role": "user", "content": f"Email:\nhi\n\nReply:\n{reply}\n\nArticles:\nkb-101"},
        ]
        body = {"model": "mock-judge", "messages": messages}
        response = llm.post("/v1/chat/completions", json=body).json()
        result: dict[str, object] = _json.loads(response["choices"][0]["message"]["content"])
        return result

    assert all(ask("Please follow kb-101.")[k] for k in ("answers", "grounded", "language", "safe"))
    assert ask("")["answers"] is False


hooks = TestClient(mock_webhook.app)


def signed(
    body: dict[str, object],
    secret: str = "dev-webhook-secret",  # noqa: S107 - the mock's default secret
) -> tuple[bytes, dict[str, str]]:
    raw = _json.dumps(body).encode()
    return raw, {
        "x-mappd-signature": sign(secret, raw, int(time.time())),
        "content-type": "application/json",
    }


def test_webhook_mock_records_verified_deliveries_once() -> None:
    hooks.post("/reset")
    event = {"id": "support:m-1:created:0", "type": "created", "case_id": "m-1", "version": 0}
    raw, headers = signed(event)
    assert hooks.post("/hook", content=raw, headers=headers).status_code == 200
    assert hooks.post("/hook", content=raw, headers=headers).status_code == 200
    assert hooks.get("/deliveries").json() == [event]


def test_webhook_mock_stages_an_outage_and_counts_hits() -> None:
    hooks.post("/reset")
    event = {"id": "support:m-2:created:0", "type": "created", "case_id": "m-2", "version": 0}
    raw, headers = signed(event)
    hooks.post("/down")
    assert hooks.post("/hook", content=raw, headers=headers).status_code == 503
    assert hooks.get("/hits").json() == {}, "a refused delivery is not a hit"
    hooks.post("/up")
    assert hooks.post("/hook", content=raw, headers=headers).status_code == 200
    assert hooks.post("/hook", content=raw, headers=headers).status_code == 200
    assert hooks.get("/hits").json() == {event["id"]: 2}
    hooks.post("/down")
    hooks.post("/reset")
    assert hooks.post("/hook", content=raw, headers=headers).status_code == 200, "reset is up"


def test_webhook_mock_reads_its_secret_from_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MAPPD_SUPPORT_WEBHOOK_SECRET=from-dotenv\n")
    monkeypatch.setenv("MAPPD_ENV_FILE", str(env_file))
    monkeypatch.delenv("MAPPD_SUPPORT_WEBHOOK_SECRET", raising=False)
    hooks.post("/reset")
    event = {"id": "support:m-3:created:0", "type": "created", "case_id": "m-3", "version": 0}
    raw, headers = signed(event, secret="from-dotenv")  # noqa: S106 - a test value
    assert hooks.post("/hook", content=raw, headers=headers).status_code == 200


def test_webhook_mock_refuses_a_bad_signature() -> None:
    raw, headers = signed({"id": "x"}, secret="wrong")  # noqa: S106 - a test value
    assert hooks.post("/hook", content=raw, headers=headers).status_code == 401


KB = "\n".join(
    [
        "kb-101: Sensor shows offline — Bus sensors (Temperature Sensor T100, Motion Sensor"
        " M110): check the bus cable and power-cycle the controller.",
        "kb-107: Re-pairing a Zigbee device — Hold the device button for 10 seconds until it"
        " blinks, then choose Add device in the app.",
        "kb-109: Setting up alerts — Create a rule under Automations; choose the sensor, a"
        " threshold and who gets notified.",
    ]
)


@pytest.mark.parametrize(
    ("ticket", "cited"),
    [
        ("How do I pair my Zigbee device again?", "kb-107"),  # pair ~ Re-pairing
        ("Please reset a motion sensor", "kb-101"),  # "a" counts for nothing
        ("I want to set up an alert", "kb-109"),  # alert ~ alerts
    ],
)
def test_the_closest_article_is_found_by_stems_without_stopwords(ticket: str, cited: str) -> None:
    article = mock_llm._best_article(ticket, KB)
    assert article is not None
    assert article[0] == cited


@pytest.mark.parametrize(
    ("ticket", "cited"),
    [
        ("Pair my Zigbee device?", "kb-107"),
        ("How do I reset a motion sensor?", "kb-101"),
        ("The recorder storage is full", "kb-106"),
    ],
)
def test_search_then_choice_finds_the_article_end_to_end(ticket: str, cited: str) -> None:
    # As the app does it: the seed embeds "title body", search_kb returns the 3 nearest, and
    # the drafter chooses among those 3 only.
    from processes.support.data.seed import load_articles  # noqa: PLC0415

    async def collect() -> list[Any]:
        return [a async for a in load_articles()]

    articles = asyncio.run(collect())
    query = mock_llm._embed(ticket)

    def distance(a: Any) -> float:
        vector = mock_llm._embed(f"{a.title} {a.body}")
        return 1 - sum(x * y for x, y in zip(query, vector, strict=True))

    nearest = sorted(articles, key=lambda a: (distance(a), a.id))[:3]
    assert cited in [a.id for a in nearest], "the search returns it"
    found = "\n".join(f"{a.id}: {a.title} — {a.body}" for a in nearest)
    article = mock_llm._best_article(ticket, found)
    assert article is not None
    assert article[0] == cited, "and the drafter cites it"
