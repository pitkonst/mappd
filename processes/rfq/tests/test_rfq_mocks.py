import json

from fastapi.testclient import TestClient

from processes.rfq.mocks import mock_llm
from processes.rfq.schemas import Extraction

client = TestClient(mock_llm.app)
EXTRACT = {
    "model": "mock-smart",
    "messages": [
        {"role": "system", "content": "You extract requests for quotation for Helio"},
        {
            "role": "user",
            "content": (
                "Subject: Quote\n\nPlease quote 12 x Temperature Sensor T100"
                " and a dozen Water Leak Sensor W20."
            ),
        },
    ],
    "tools": [
        {"type": "function", "function": {"name": "search_catalog"}},
        {"type": "function", "function": {"name": "final_result"}},
    ],
}


def test_extractor_answers_an_extraction() -> None:
    reply = client.post("/v1/chat/completions", json=EXTRACT).json()
    call = reply["choices"][0]["message"]["tool_calls"][0]["function"]
    assert call["name"] == "final_result"
    extraction = Extraction.model_validate(json.loads(call["arguments"]))
    assert {(ln.sku, ln.quantity) for ln in extraction.lines} == {("SEN-T100", 12), ("SEN-W20", 12)}
    assert extraction.custom_request is False


def test_claims_only_rfq_requests() -> None:
    assert mock_llm.claims(EXTRACT)
    assert mock_llm.claims({"__path__": "/rfq/calls"})
    support = {
        "model": "mock-fast",
        "messages": [{"role": "user", "content": "Sensor offline"}],
        "tools": [{"type": "function", "function": {"name": "final_result"}}],
    }
    assert not mock_llm.claims(support)
    assert not mock_llm.claims({"__path__": "/calls"})


def test_checker_returns_lines_key() -> None:
    body = {
        "model": "mock-check",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Read the customer email and return only a JSON object with the keys"
                    " outcome, language, lines_key."
                ),
            },
            {"role": "user", "content": "Subject: Q\n\nWe need 12 x Temperature Sensor T100."},
        ],
    }
    assert mock_llm.claims(body)
    content = client.post("/v1/chat/completions", json=body).json()["choices"][0]["message"][
        "content"
    ]
    assert json.loads(content) == {"outcome": "quote", "language": "en", "lines_key": "SEN-T100x12"}


def test_an_items_name_stops_before_when_it_is_needed() -> None:
    text = "Please quote 10 x SEN-C300 CO2 sensors delivered within 7 days."
    [line] = mock_llm.read(text)["lines"]
    assert line["requested"] == "SEN-C300 CO2 sensors"
    assert line["sku"] == "SEN-C300"
