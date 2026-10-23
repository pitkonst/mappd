"""The turn engine: one message in, the job's next state, reply, resolution and tool calls out."""

from typing import Any

import dbos_async
import httpx2
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from mappd import mcp
from processes.installation import engine, tools
from processes.installation.engine import JobView, ToolCall
from processes.installation.mocks import mock_mcp
from processes.installation.mocks.systems import Facts, Systems
from processes.installation.mocks.systems import toolset as systems_toolset
from processes.installation.schemas import Interpretation, Proposal, Resolution, State

JOB = JobView(
    external_id="j-1", state="access", skus=["CTL-H400"], site="Riverside Offices", turn_key="7"
)


def reading(intent: str, *, reached: bool = False, exception: str | None = None) -> Any:
    async def interpret(
        job: JobView, nxt: State | None, text: str, photo: str | None
    ) -> Interpretation:
        return Interpretation.model_validate(
            {"intent": intent, "reached_next": reached, "exception": exception, "reply": "ok"}
        )

    return interpret


class Resolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self, job: JobView, reason: str, history: list[str]
    ) -> tuple[Resolution, list[ToolCall]]:
        self.calls.append((job.external_id, reason))
        proposal = Proposal(
            tool="reserve_parts", arguments={"job": job.external_id, "skus": job.skus}, reason="r"
        )
        resolution = Resolution(
            summary="s", reply="A dispatcher will confirm.", proposals=[proposal]
        )
        call = ToolCall(tool="check_stock", arguments={"sku": "CTL-H400"}, result={"on_hand": 1})
        return resolution, [call]


def turn(
    job: JobView, text: str, photo: str | None, interpret: Any, resolver: engine.Resolve
) -> Any:
    return dbos_async.run(engine.turn(job, text, photo, interpret=interpret, resolve=resolver))


def test_progress_moves_one_step_without_the_resolver() -> None:
    resolver = Resolver()
    got = turn(JOB, "found it", None, reading("progress", reached=True), resolver)
    assert (got.state, got.event, got.reply, got.resolution) == (
        "equipment_found",
        "progress",
        "ok",
        None,
    )
    assert resolver.calls == []


def test_evidence_is_refused_without_a_photo() -> None:
    job = JOB.model_copy(update={"state": "equipment_found"})
    got = turn(job, "installed", None, reading("progress", reached=True), Resolver())
    assert got.state == "equipment_found"
    assert "photo" in got.reply.lower()


def test_an_exception_blocks_and_the_resolution_replies() -> None:
    resolver = Resolver()
    got = turn(
        JOB, "parts missing", None, reading("exception", exception="missing_parts"), resolver
    )
    assert (got.state, got.blocked_from, got.blocked_reason, got.event) == (
        "blocked",
        "access",
        "missing_parts",
        "blocked",
    )
    assert got.reply == "A dispatcher will confirm."
    assert got.resolution is not None
    assert got.resolution.proposals[0].tool == "reserve_parts"
    assert [c.tool for c in got.tool_calls] == ["check_stock"]
    assert resolver.calls == [("j-1", "missing_parts")]


class GivingUp(Resolver):
    """A resolver whose model kept calling tools until its request limit stopped it."""

    async def __call__(
        self, job: JobView, reason: str, history: list[str]
    ) -> tuple[Resolution, list[ToolCall]]:
        self.calls.append((job.external_id, reason))
        raise UsageLimitExceeded("The next request would exceed the request_limit of 12")


def test_a_resolver_that_gives_up_still_blocks_the_job_for_a_dispatcher() -> None:
    # A real model once called check_stock 25 times until its limit: the update failed, the
    # installer got no answer and the job never showed as blocked.
    resolver = GivingUp()
    got = turn(
        JOB, "parts missing", None, reading("exception", exception="missing_parts"), resolver
    )
    assert (got.state, got.blocked_from, got.blocked_reason, got.event) == (
        "blocked",
        "access",
        "missing_parts",
        "blocked",
    )
    assert got.reply == "ok"  # the interpreter's acknowledgement, in the installer's language
    assert got.resolution is not None
    assert got.resolution.proposals == []
    assert "could not finish" in got.resolution.summary
    assert got.tool_calls == []
    assert resolver.calls == [("j-1", "missing_parts")]


def test_an_incident_proposes_no_parts_or_deliveries() -> None:
    # The resolver still reads it and answers, but nothing is reserved or booked for an incident.
    resolver = Resolver()
    reading = reading_with_incident()
    got = turn(JOB, "I forced my way past the guard", None, reading, resolver)
    assert (got.state, got.blocked_reason) == ("blocked", "incident")
    assert resolver.calls == [("j-1", "incident")]
    assert got.resolution is not None
    assert got.resolution.proposals == []
    assert got.reply == "A dispatcher will confirm."


def reading_with_incident() -> Any:
    async def interpret(
        job: JobView, nxt: State | None, text: str, photo: str | None
    ) -> Interpretation:
        return Interpretation.model_validate(
            {"intent": "progress", "reached_next": True, "incident": True, "reply": "ok"}
        )

    return interpret


def test_an_interpreter_that_fails_hands_the_message_to_a_dispatcher() -> None:
    # A real model gave up on "It is CTL-H500 here, must be a mistake": no reply, no block.
    async def failing(
        job: JobView, nxt: State | None, text: str, photo: str | None
    ) -> Interpretation:
        raise UnexpectedModelBehavior("Exceeded maximum output retries (3)")

    resolver = Resolver()
    got = turn(JOB, "It is CTL-H500 here. Must be a mistake", None, failing, resolver)
    assert (got.state, got.blocked_from, got.blocked_reason, got.event) == (
        "blocked",
        "access",
        "other",
        "blocked",
    )
    assert got.reply == engine.UNREAD_REPLY
    assert got.resolution is not None
    assert "could not read" in got.resolution.summary
    assert got.resolution.proposals == []
    assert resolver.calls == []


class RunsOutOfRetries(Resolver):
    async def __call__(
        self, job: JobView, reason: str, history: list[str]
    ) -> tuple[Resolution, list[ToolCall]]:
        self.calls.append((job.external_id, reason))
        raise UnexpectedModelBehavior("Exceeded maximum output retries (3)")


def test_a_resolver_that_runs_out_of_retries_still_blocks_the_job() -> None:
    got = turn(
        JOB,
        "parts missing",
        None,
        reading("exception", exception="missing_parts"),
        RunsOutOfRetries(),
    )
    assert (got.state, got.blocked_reason, got.reply) == ("blocked", "missing_parts", "ok")
    assert got.resolution is not None
    assert "could not finish" in got.resolution.summary


def test_the_resolver_replies_in_the_language_the_interpreter_read() -> None:
    # The resolver wrote German to English-speaking installers in 4 of 49 eval cases.
    seen: list[str] = []
    resolver = Resolver()

    async def resolve(
        job: JobView, reason: str, history: list[str]
    ) -> tuple[Resolution, list[ToolCall]]:
        seen.append(job.language)
        return await resolver(job, reason, history)

    async def interpret(
        job: JobView, nxt: State | None, text: str, photo: str | None
    ) -> Interpretation:
        return Interpretation.model_validate(
            {
                "intent": "exception",
                "reached_next": False,
                "exception": "missing_parts",
                "reply": "ok",
                "language": "en",
            }
        )

    turn(JOB, "the sensor is missing", None, interpret, resolve)
    assert seen == ["en"]


def test_blocked_and_closed_jobs_never_call_the_resolver() -> None:
    resolver = Resolver()
    for state in ("blocked", "handed_over"):
        job = JOB.model_copy(update={"state": state})
        got = turn(job, "broken", None, reading("exception", exception="breakage"), resolver)
        assert got.state == state
    assert resolver.calls == []


def test_tool_calls_pair_calls_with_returns_and_skip_the_output_tool() -> None:
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("go")]),
        ModelResponse(parts=[ToolCallPart("check_stock", {"sku": "A"}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart("check_stock", {"on_hand": 2}, tool_call_id="c1")]),
        ModelResponse(parts=[ToolCallPart("final_result", {"summary": "s"}, tool_call_id="c2")]),
    ]
    assert engine.tool_calls(messages) == [
        ToolCall(tool="check_stock", arguments={"sku": "A"}, result={"on_hand": 2})
    ]


def test_resolver_toolset_has_no_confirmed_tools() -> None:
    async def names() -> tuple[list[str], list[str]]:
        served = mock_mcp.create_app()
        async with mock_mcp.running(served):
            transport = httpx2.ASGITransport(app=served)
            over_mcp = tools.for_resolver(
                mcp.toolset("http://mock-mcp/warehouse/mcp", id="w", transport=transport)
            )
            seen_mcp = await _tool_names(over_mcp)
        in_process = tools.for_resolver(systems_toolset(Systems(Facts()), include_confirmed=True))
        return seen_mcp, await _tool_names(in_process)

    over_mcp, in_process = dbos_async.run(names())
    assert over_mcp == ["check_stock"]
    assert in_process == ["check_stock", "delivery_slots", "open_ticket", "site_contact"]


async def _tool_names(toolset: Any) -> list[str]:
    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("done")])

    agent: Agent[JobView, str] = Agent(FunctionModel(model), deps_type=JobView, toolsets=[toolset])
    async with toolset:
        await agent.run("go", deps=JOB)
    return sorted(seen)


def test_the_key_is_injected_from_the_turn_not_the_model() -> None:
    systems = Systems(Facts())
    toolset = tools.for_resolver(systems_toolset(systems))
    schemas: dict[str, Any] = {}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        schemas.update({t.name: t.parameters_json_schema for t in info.function_tools})
        if len(messages) == 1:
            args = {"job": "j-1", "kind": "breakage", "text": "cracked"}
            return ModelResponse(parts=[ToolCallPart("open_ticket", args)])
        return ModelResponse(parts=[TextPart("done")])

    agent: Agent[JobView, str] = Agent(FunctionModel(model), deps_type=JobView, toolsets=[toolset])
    dbos_async.run(agent.run("go", deps=JOB))
    assert "key" not in schemas["open_ticket"]["properties"]
    assert systems.calls == [
        (
            "open_ticket",
            {"job": "j-1", "kind": "breakage", "text": "cracked", "key": "j-1:7:open_ticket"},
        )
    ]


def test_a_mutating_tool_without_a_key_is_refused() -> None:
    systems = Systems(Facts())
    assert systems.open_ticket("j-1", "breakage", "x")["ok"] is False
    assert systems.calls[0][0] == "open_ticket"


def test_the_resolver_reads_the_message_that_blocked_the_job() -> None:
    resolver = Resolver()
    seen: list[list[str]] = []

    async def resolve(
        job: JobView, reason: str, history: list[str]
    ) -> tuple[Resolution, list[ToolCall]]:
        seen.append(history)
        return await resolver(job, reason, history)

    job = JOB.model_copy(update={"history": ["installer: on site", "bot: ok"]})
    dbos_async.run(
        engine.turn(
            job,
            "the CTL-H400 is cracked",
            "a cracked housing",
            interpret=reading("exception", exception="breakage"),
            resolve=resolve,
        )
    )
    assert seen == [
        [
            "installer: on site",
            "bot: ok",
            "installer: the CTL-H400 is cracked [photo: a cracked housing]",
        ]
    ]


def test_the_resolver_sees_only_the_allowed_tools() -> None:
    from pydantic_ai.toolsets import FunctionToolset  # noqa: PLC0415

    def check_stock(sku: str) -> dict[str, Any]:
        return {"sku": sku}

    def warehouse_reserve_parts(job: str, skus: list[str]) -> dict[str, Any]:
        return {}

    def cancel_order(job: str) -> dict[str, Any]:
        return {}

    toolset: FunctionToolset[Any] = FunctionToolset(
        tools=[check_stock, warehouse_reserve_parts, cancel_order]
    )
    assert dbos_async.run(_tool_names(tools.for_resolver(toolset))) == ["check_stock"]
    assert frozenset({"check_stock", "delivery_slots", "open_ticket", "site_contact"}) == (
        tools.RESOLVER_TOOLS
    )


def test_a_turn_sees_the_stored_job_with_its_appointment() -> None:
    from datetime import UTC, datetime  # noqa: PLC0415

    from processes.installation.schemas import JobRecord  # noqa: PLC0415

    record = JobRecord(
        id=1,
        external_id="j-1",
        installer_id=2,
        site="Lindenstrasse 7",
        skus=["CTL-H400"],
        state="access",
        blocked_from=None,
        version=3,
        appointment_at=datetime(2026, 10, 24, 9, 0, tzinfo=UTC),
        history=["installer: hi"],
    )
    got = engine.view(record, "456")
    assert (got.external_id, got.state, got.skus, got.site) == (
        "j-1",
        "access",
        ["CTL-H400"],
        "Lindenstrasse 7",
    )
    assert got.appointment == "2026-10-24 09:00 UTC"
    assert (got.history, got.turn_key) == (["installer: hi"], "456")
    assert engine.view(record.model_copy(update={"appointment_at": None}), "1").appointment == ""
