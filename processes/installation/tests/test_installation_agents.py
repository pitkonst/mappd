import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from dbos_async import run as arun
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from mappd import telemetry
from processes.installation import vision
from processes.installation.agents.interpreter import INSTRUCTIONS, interpret, interpreter
from processes.installation.agents.resolver import mcp_url
from processes.installation.mocks import mock_llm


def test_the_prompt_starts_with_the_mock_marker() -> None:
    assert INSTRUCTIONS.startswith(mock_llm.INTERPRETER_MARK)


def test_interpret_passes_the_state_and_the_photo() -> None:
    seen: list[str] = []

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(str(messages[-1]))
        args = {"intent": "progress", "reached_next": True, "exception": None, "reply": "ok"}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    from processes.installation.engine import JobView  # noqa: PLC0415

    job = JobView(external_id="j-1", state="equipment_found", skus=["CTL-H400"], site="S")
    with interpreter.override(model=FunctionModel(answer)):
        got = arun(interpret(job, "installed", "done", "a mounted controller"))
    assert (got.intent, got.reached_next) == ("progress", True)
    assert "Current step: equipment_found" in seen[0]
    assert "Next step: installed" in seen[0]
    assert "Photo: a mounted controller" in seen[0]


def test_the_interpreter_reads_the_job_and_the_conversation() -> None:
    # Without them it listed equipment the job did not have, forgot that the technician asked
    # for English, and could not say when the appointment was.
    from processes.installation.agents.interpreter import message_text  # noqa: PLC0415
    from processes.installation.engine import JobView  # noqa: PLC0415

    job = JobView(
        external_id="j-1",
        state="access",
        skus=["CTL-H400", "SEN-T100"],
        site="Lindenstrasse 7",
        appointment="2026-10-24 09:00 UTC",
        history=["installer: In english please", "bot: Of course."],
    )
    text = message_text(job, "equipment_found", "What equipment?", None)
    assert "Site: Lindenstrasse 7" in text
    assert "Equipment: CTL-H400, SEN-T100" in text
    assert "Appointment: 2026-10-24 09:00 UTC" in text
    assert "- installer: In english please" in text
    assert "Current step: access" in text
    assert text.rstrip().endswith("Message: What equipment?")


def test_interpreter_is_named() -> None:
    assert interpreter.name == "installation.interpreter"


def test_describe_sends_a_data_url_to_the_vision_model(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json={"choices": [{"message": {"content": "a controller"}}]})

    monkeypatch.setattr(
        vision, "HTTP", telemetry.traced_client(transport=httpx2.MockTransport(handler))
    )
    assert vision.describe_image(b"\xff\xd8JPEG") == "a controller"
    body = sent[0]
    assert body["model"] == "vision"
    parts = body["messages"][0]["content"]
    assert parts[0]["text"].startswith(mock_llm.VISION_MARK)
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def mock_resolver(systems: Any) -> Any:
    """A plain resolver whose model is the mock LLM, over in-process systems."""
    from mappd import gateway  # noqa: PLC0415
    from processes.installation.agents import resolver  # noqa: PLC0415
    from processes.installation.mocks.systems import toolset  # noqa: PLC0415

    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=mock_llm.app))
    model = gateway.model("smart", client=client)
    return resolver.build([toolset(systems)], durable_runs=False, model=model)


def resolve(systems: Any, reason: str, skus: list[str]) -> Any:
    from processes.installation.agents import resolver  # noqa: PLC0415
    from processes.installation.engine import JobView  # noqa: PLC0415

    job = JobView(
        external_id="j-1", state="access", skus=skus, site="Riverside Offices", turn_key="5"
    )
    return arun(resolver.run(mock_resolver(systems), job, reason, ["installer: hello"]))


def test_the_resolver_prompt_starts_with_the_mock_marker() -> None:
    from processes.installation.agents.resolver import INSTRUCTIONS as RESOLVER  # noqa: PLC0415

    assert RESOLVER.startswith(mock_llm.RESOLVER_MARK)


def test_the_resolver_reads_the_job_and_the_conversation() -> None:
    from processes.installation.agents import resolver  # noqa: PLC0415
    from processes.installation.engine import JobView  # noqa: PLC0415

    job = JobView(external_id="j-1", state="access", skus=["CTL-H400"], site="Riverside Offices")
    text = resolver.message_text(job, "missing_parts", ["installer: no controller here"])
    assert "Job: j-1" in text
    assert "Equipment: CTL-H400" in text
    assert "Problem: missing_parts" in text
    assert "- installer: no controller here" in text


def test_a_missing_part_in_stock_is_proposed_for_reservation() -> None:
    from processes.installation.mocks.systems import Facts, Systems  # noqa: PLC0415

    systems = Systems(Facts(stock={"CTL-H400": 2}))
    resolution, calls = resolve(systems, "missing_parts", ["CTL-H400"])
    assert [(p.tool, p.arguments) for p in resolution.proposals] == [
        ("reserve_parts", {"skus": ["CTL-H400"]})
    ]
    assert [c.tool for c in calls] == ["check_stock"]
    assert systems.calls[0] == ("check_stock", {"sku": "CTL-H400"})


def test_a_missing_part_out_of_stock_is_proposed_for_delivery() -> None:
    from processes.installation.mocks.systems import Facts, Slot, Systems  # noqa: PLC0415

    systems = Systems(Facts(slots=[Slot(at="2026-10-23T09:00")]))
    resolution, calls = resolve(systems, "missing_parts", ["CTL-H400"])
    assert [(p.tool, p.arguments) for p in resolution.proposals] == [
        ("book_delivery", {"slot": "2026-10-23T09:00"})
    ]
    assert [c.tool for c in calls] == ["check_stock", "delivery_slots"]


def test_breakage_opens_a_ticket_with_the_turns_key() -> None:
    from processes.installation.mocks.systems import Facts, Systems  # noqa: PLC0415

    systems = Systems(Facts(stock={"CTL-H400": 1}))
    resolution, _ = resolve(systems, "breakage", ["CTL-H400"])
    tickets = [args for name, args in systems.calls if name == "open_ticket"]
    assert [t["key"] for t in tickets] == ["j-1:5:open_ticket"]
    assert [p.tool for p in resolution.proposals] == ["reserve_parts"]


def test_no_access_gives_the_site_contact() -> None:
    from processes.installation.mocks.systems import Contact, Facts, Systems  # noqa: PLC0415

    contact = Contact(name="Marta Novak", phone="+49 000 5550 1201", access="Key at the desk.")
    systems = Systems(Facts(contacts={"Riverside Offices": contact}))
    resolution, _ = resolve(systems, "no_access", ["CTL-H400"])
    assert resolution.proposals == []
    assert "Marta Novak" in resolution.reply
    assert "+49 000 5550 1201" in resolution.reply


def test_the_mcp_url_is_read_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MAPPD_INSTALLATION_MCP_URL=http://gateway.example:3000/mcp\n")
    monkeypatch.setenv("MAPPD_ENV_FILE", str(env_file))
    monkeypatch.delenv("MAPPD_INSTALLATION_MCP_URL", raising=False)
    assert mcp_url() == "http://gateway.example:3000/mcp"


def test_a_resolver_that_keeps_calling_tools_stops_at_its_own_limit() -> None:
    # A real model once called check_stock 25 times, until pydantic-ai's default 50 requests.
    from pydantic_ai.exceptions import UsageLimitExceeded  # noqa: PLC0415

    from processes.installation.agents import resolver  # noqa: PLC0415
    from processes.installation.engine import JobView  # noqa: PLC0415
    from processes.installation.mocks.systems import Facts, Systems, toolset  # noqa: PLC0415

    requests: list[int] = []

    def loop(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        requests.append(len(messages))
        return ModelResponse(parts=[ToolCallPart("check_stock", {"sku": "SEN-T100"})])

    systems = Systems(Facts(stock={"SEN-T100": 55}))
    agent = resolver.build([toolset(systems)], durable_runs=False, model=FunctionModel(loop))
    job = JobView(external_id="j-1", state="access", skus=["SEN-T100"], site="Riverside Offices")
    with pytest.raises(UsageLimitExceeded):
        arun(resolver.run(agent, job, "missing_parts", ["installer: sensors missing"]))
    assert len(requests) == resolver.REQUEST_LIMIT


def test_the_resolver_is_told_the_reply_language() -> None:
    from processes.installation.agents import resolver  # noqa: PLC0415
    from processes.installation.engine import JobView  # noqa: PLC0415

    job = JobView(external_id="j-1", state="access", skus=["CTL-H400"], site="S", language="en")
    assert "Reply language: en" in resolver.message_text(job, "missing_parts", ["installer: hi"])
    unknown = job.model_copy(update={"language": ""})
    assert "Reply language" not in resolver.message_text(unknown, "missing_parts", [])
