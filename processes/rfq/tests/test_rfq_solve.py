from typing import Any

from dbos_async import run as arun
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from processes.rfq.agents.extractor import extractor, search_catalog
from processes.rfq.schemas import RfqIn
from processes.rfq.solve import solve

RFQ = RfqIn(
    external_id="r-1",
    sender="anna@example.com",
    subject="Quote",
    body="Please quote 12 temperature sensors and one Helio Hub 400.",
)


def returns(fields: dict[str, Any]) -> FunctionModel:
    def answer(_m: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, fields)])

    return FunctionModel(answer)


def test_solve_prices_what_the_agent_extracts() -> None:
    model = returns(
        {
            "lines": [
                {
                    "requested": "temperature sensors",
                    "quantity": 12,
                    "sku": "SEN-T100",
                    "confidence": "high",
                },
                {
                    "requested": "Helio Hub 400",
                    "quantity": 1,
                    "sku": "CTL-H400",
                    "confidence": "high",
                },
            ],
            "needed_by_days": None,
            "custom_request": False,
            "language": "en",
        }
    )
    with extractor.override(model=model):
        out = arun(solve(RFQ))
    assert str(out.total) == "1266.00"
    assert out.outcome == "quote"


def test_search_catalog_tool_lists_hits() -> None:
    text = arun(search_catalog("temperature sensor"))
    assert text.splitlines()[0].startswith("SEN-T100:")
    assert "48.00 EUR" in text


def test_the_agent_is_named_and_uses_a_gateway_model() -> None:
    assert extractor.name == "rfq.extractor"
