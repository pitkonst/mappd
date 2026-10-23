import asyncio
import json
from typing import Any

import httpx2
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel
from pydantic_ai import Agent

from mappd import telemetry
from mappd.settings import Settings
from mappd.telemetry import IdleTraceFilter, idle

COMPLETION = {
    "id": "c-1",
    "object": "chat.completion",
    "created": 0,
    "model": "fast",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


@pytest.fixture
def spans() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_traced_transport_injects_traceparent_and_records_a_client_span(
    spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = spans
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={})

    transport = telemetry.TracedTransport(httpx2.MockTransport(handler), tracer_provider=provider)

    async def call() -> None:
        async with httpx2.AsyncClient(transport=transport) as client:
            with provider.get_tracer("test").start_as_current_span("workflow"):
                await client.get("http://gateway/v1/models")

    asyncio.run(call())
    [request] = seen
    parent, client_span = sorted(exporter.get_finished_spans(), key=lambda s: s.name != "workflow")
    assert client_span.name == "GET /v1/models"
    assert client_span.kind == trace.SpanKind.CLIENT
    assert client_span.parent is not None
    assert client_span.parent.span_id == parent.context.span_id
    trace_id = format(parent.context.trace_id, "032x")
    assert request.headers["traceparent"].split("-")[1] == trace_id


def test_traced_sync_transport_injects_traceparent_and_records_a_client_span(
    spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = spans
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={})

    transport = telemetry.TracedSyncTransport(
        httpx2.MockTransport(handler), tracer_provider=provider
    )
    with (
        httpx2.Client(transport=transport) as client,
        provider.get_tracer("test").start_as_current_span("workflow"),
    ):
        client.post("http://mailer/send", json={})

    [request] = seen
    parent, client_span = sorted(exporter.get_finished_spans(), key=lambda s: s.name != "workflow")
    assert client_span.name == "POST /send"
    assert client_span.kind == trace.SpanKind.CLIENT
    assert client_span.parent is not None
    assert client_span.parent.span_id == parent.context.span_id
    trace_id = format(parent.context.trace_id, "032x")
    assert request.headers["traceparent"].split("-")[1] == trace_id


def test_traced_sync_transport_marks_5xx_as_error(
    spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = spans
    transport = telemetry.TracedSyncTransport(
        httpx2.MockTransport(lambda _: httpx2.Response(503)), tracer_provider=provider
    )
    with httpx2.Client(transport=transport) as client:
        client.post("http://mailer/send")

    [span] = exporter.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.attributes is not None
    assert span.attributes["http.response.status_code"] == 503
    assert span.attributes["error.type"] == "503"


def test_traced_client_requests_join_the_current_trace(
    spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = spans
    client = telemetry.traced_client(
        timeout=5,
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json={})),
        tracer_provider=provider,
    )
    with client, provider.get_tracer("test").start_as_current_span("workflow"):
        client.get("http://mailer/health")

    by_name = {s.name: s for s in exporter.get_finished_spans()}
    span, parent = by_name["GET /health"], by_name["workflow"]
    assert span.parent is not None
    assert span.parent.span_id == parent.context.span_id


def test_gateway_5xx_marks_the_span_as_error(
    spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = spans
    transport = telemetry.TracedTransport(
        httpx2.MockTransport(lambda _: httpx2.Response(503)), tracer_provider=provider
    )

    async def call() -> None:
        async with httpx2.AsyncClient(transport=transport) as client:
            await client.post("http://gateway/v1/chat/completions")

    asyncio.run(call())
    [span] = exporter.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.attributes is not None
    assert span.attributes["http.response.status_code"] == 503


def test_setup_exports_to_mlflow_with_the_experiment_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[Any] = []
    monkeypatch.setattr(trace, "set_tracer_provider", installed.append)
    monkeypatch.setattr(Agent, "instrument_all", lambda *_a, **_k: None)
    exporter = InMemorySpanExporter()
    settings = Settings(app_name="acme", app_version="a1b2c3d")
    provider = telemetry.setup(settings, exporter=exporter)
    assert installed == [provider]
    assert provider.resource.attributes["service.name"] == "acme"
    assert provider.resource.attributes["service.version"] == "a1b2c3d"
    provider.get_tracer("t").start_span("s").end()
    provider.force_flush()
    assert [s.name for s in exporter.get_finished_spans()] == ["s"]
    provider.shutdown()


def test_default_exporter_targets_mlflow(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict[str, Any]] = []
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", lambda **kwargs: built.append(kwargs))
    telemetry.mlflow_exporter(Settings(mlflow_url="http://mlflow:5000", mlflow_experiment_id="7"))
    assert built == [
        {
            "endpoint": "http://mlflow:5000/v1/traces",
            "headers": {"x-mlflow-experiment-id": "7"},
        }
    ]


@pytest.mark.parametrize("status", [401, 404, 429])
def test_gateway_4xx_marks_the_span_as_error(
    spans: tuple[TracerProvider, InMemorySpanExporter], status: int
) -> None:
    provider, exporter = spans
    transport = telemetry.TracedTransport(
        httpx2.MockTransport(lambda _: httpx2.Response(status)), tracer_provider=provider
    )

    async def call() -> None:
        async with httpx2.AsyncClient(transport=transport) as client:
            await client.post("http://gateway/v1/chat/completions")

    asyncio.run(call())
    [span] = exporter.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.attributes is not None
    assert span.attributes["error.type"] == str(status)


def test_transport_failure_marks_the_span_as_error(
    spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = spans

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectTimeout("gateway timed out", request=request)

    transport = telemetry.TracedTransport(httpx2.MockTransport(handler), tracer_provider=provider)

    async def call() -> None:
        async with httpx2.AsyncClient(transport=transport) as client:
            await client.get("http://gateway/v1/models")

    with pytest.raises(httpx2.ConnectTimeout):
        asyncio.run(call())
    [span] = exporter.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR
    assert [e.name for e in span.events] == ["exception"]


def test_mlflow_span_exporter_enriches_chat_agent_and_dbos_spans() -> None:
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(telemetry.MlflowSpanExporter(inner)))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span(
        "support.process_ticket", attributes={"operationType": "workflow"}
    ):
        telemetry.annotate_trace(
            name="support.process_ticket",
            inputs={"external_id": "t-1"},
            outputs={"status": "awaiting_approval"},
            user="anna@example.com",
            session="t-1",
            tags={"ticket.external_id": "t-1"},
        )
        with tracer.start_as_current_span(
            "support.load_ticket", attributes={"operationType": "step"}
        ):
            pass
        with tracer.start_as_current_span(
            "invoke_agent support.drafter",
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.conversation.id": "random-uuid",
                "gen_ai.system_instructions": json.dumps([{"type": "text", "content": "Use KB."}]),
                "pydantic_ai.all_messages": json.dumps(
                    [
                        {"role": "user", "parts": [{"type": "text", "content": "Sensor offline"}]},
                        {
                            "role": "assistant",
                            "parts": [
                                {"type": "thinking", "content": "Searching KB."},
                                {
                                    "type": "tool_call",
                                    "id": "c1",
                                    "name": "search_kb",
                                    "arguments": '{"query": "offline"}',
                                },
                            ],
                        },
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "type": "tool_call_response",
                                    "id": "c1",
                                    "name": "search_kb",
                                    "result": "kb-101: Check cable.",
                                }
                            ],
                        },
                    ]
                ),
                "final_result": json.dumps({"reply": "Check cable.", "citations": ["kb-101"]}),
            },
        ):
            pass
        with tracer.start_as_current_span(
            "execute_tool search_kb",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "search_kb",
                "gen_ai.tool.call.arguments": '{"query": "offline"}',
                "gen_ai.tool.call.result": "kb-101: Check cable.",
            },
        ):
            pass
        with tracer.start_as_current_span(
            "chat smart",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.conversation.id": "random-uuid",
                "gen_ai.system_instructions": json.dumps([{"type": "text", "content": "Use KB."}]),
                "gen_ai.input.messages": json.dumps(
                    [{"role": "user", "parts": [{"type": "text", "content": "Sensor offline"}]}]
                ),
                "gen_ai.output.messages": json.dumps(
                    [
                        {
                            "role": "assistant",
                            "parts": [
                                {
                                    "type": "tool_call",
                                    "id": "c1",
                                    "name": "search_kb",
                                    "arguments": {"query": "offline"},
                                }
                            ],
                        }
                    ]
                ),
                "gen_ai.tool.definitions": json.dumps(
                    [
                        {
                            "type": "function",
                            "name": "search_kb",
                            "description": "Search KB",
                            "parameters": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                        }
                    ]
                ),
            },
        ):
            pass

    by_name = {s.name: dict(s.attributes or {}) for s in inner.get_finished_spans()}
    wf = by_name["support.process_ticket"]
    assert wf["mlflow.spanType"] == "WORKFLOW"
    assert wf["mlflow.traceTag.mlflow.traceName"] == "support.process_ticket"
    assert wf["user.id"] == "anna@example.com"
    assert wf["session.id"] == "t-1"
    assert wf["mlflow.traceTag.ticket.external_id"] == "t-1"
    assert json.loads(str(wf["mlflow.spanInputs"])) == {"external_id": "t-1"}
    assert json.loads(str(wf["mlflow.spanOutputs"])) == {"status": "awaiting_approval"}
    assert by_name["support.load_ticket"]["mlflow.spanType"] == "TASK"

    agent_attrs = by_name["invoke_agent support.drafter"]
    assert "gen_ai.conversation.id" not in agent_attrs
    assert json.loads(str(agent_attrs["mlflow.spanInputs"])) == {"prompt": "Sensor offline"}
    assert json.loads(str(agent_attrs["mlflow.spanOutputs"])) == {
        "reply": "Check cable.",
        "citations": ["kb-101"],
    }
    agent_msgs = json.loads(str(agent_attrs["mlflow.chat.messages"]))
    assert [m["role"] for m in agent_msgs] == ["system", "user", "assistant", "tool"]
    assert agent_msgs[2]["reasoning"] == "Searching KB."
    assert agent_msgs[2]["tool_calls"][0]["function"]["name"] == "search_kb"
    assert agent_msgs[3]["content"] == "kb-101: Check cable."

    tool_attrs = by_name["execute_tool search_kb"]
    assert tool_attrs["mlflow.spanType"] == "TOOL"
    assert json.loads(str(tool_attrs["mlflow.spanInputs"])) == {"query": "offline"}
    assert tool_attrs["mlflow.spanOutputs"] == "kb-101: Check cable."

    chat_attrs = by_name["chat smart"]
    assert "gen_ai.conversation.id" not in chat_attrs
    tools = json.loads(str(chat_attrs["mlflow.chat.tools"]))
    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "search_kb",
                "description": "Search KB",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }
    ]
    chat_msgs = json.loads(str(chat_attrs["mlflow.chat.messages"]))
    assert [m["role"] for m in chat_msgs] == ["system", "user", "assistant"]
    assert json.loads(chat_msgs[2]["tool_calls"][0]["function"]["arguments"]) == {
        "query": "offline"
    }


class _In(BaseModel):
    external_id: str
    n: int


def test_traced_annotates_inputs_on_entry_and_outputs_on_exit(
    spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = spans
    tracer = provider.get_tracer("test")
    with (
        tracer.start_as_current_span("support.process_ticket"),
        telemetry.traced(
            "support.process_ticket",
            inputs=_In(external_id="t-1", n=2),
            user="anna@example.com",
            session="t-1",
            tags={"ticket.external_id": "t-1"},
        ) as note,
    ):
        note.outputs = {"status": "awaiting_approval"}
    [span] = exporter.get_finished_spans()
    attrs = dict(span.attributes or {})
    assert attrs["mlflow.traceTag.mlflow.traceName"] == "support.process_ticket"
    assert attrs["user.id"] == "anna@example.com"
    assert attrs["session.id"] == "t-1"
    # A pydantic input is serialized as its JSON dump.
    assert json.loads(str(attrs["mlflow.spanInputs"])) == {"external_id": "t-1", "n": 2}
    assert json.loads(str(attrs["mlflow.spanOutputs"])) == {"status": "awaiting_approval"}


def test_traced_records_no_outputs_on_error_or_when_unset(
    spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = spans
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("unset"), telemetry.traced("w"):
        pass

    def explode() -> None:
        with tracer.start_as_current_span("err"), telemetry.traced("w") as note:
            note.outputs = {"never": True}
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        explode()
    by_name = {s.name: dict(s.attributes or {}) for s in exporter.get_finished_spans()}
    assert "mlflow.spanOutputs" not in by_name["unset"]
    assert "mlflow.spanOutputs" not in by_name["err"]


def _exported(nested: bool) -> dict[str, Any]:
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(telemetry.MlflowSpanExporter(inner)))
    tracer = provider.get_tracer("test")
    child = {"operationType": "workflow", "operationUUID": "wf-child"}
    with tracer.start_as_current_span(
        "support.process_ticket", attributes={"operationType": "workflow", "operationUUID": "wf-1"}
    ):
        if nested:
            with tracer.start_as_current_span("support.child", attributes=child):
                pass
    by_name = {s.name: dict(s.attributes or {}) for s in inner.get_finished_spans()}
    return by_name["support.child" if nested else "support.process_ticket"]


def test_root_workflow_spans_carry_the_workflow_tag() -> None:
    assert _exported(nested=False)["mlflow.traceTag.mappd.workflow_id"] == "wf-1"


def test_child_workflow_spans_do_not() -> None:
    # MLflow reads trace tags from the root span only; the link uses the root workflow's ID.
    assert "mlflow.traceTag.mappd.workflow_id" not in _exported(nested=True)


def _mlflow(
    answer: httpx2.Response | Exception, seen: list[httpx2.Request]
) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx2.MockTransport(handle)


def test_find_trace_posts_the_tag_filter() -> None:
    seen: list[httpx2.Request] = []
    settings = Settings(mlflow_url="http://mlflow:5000", mlflow_experiment_id="7")
    found = telemetry.find_trace(
        settings,
        "wf-1",
        transport=_mlflow(httpx2.Response(200, json={"traces": [{"trace_id": "tr-1"}]}), seen),
    )
    assert found == "tr-1"
    [request] = seen
    assert str(request.url) == "http://mlflow:5000/api/3.0/mlflow/traces/search"
    body = json.loads(request.content)
    assert body["filter"] == 'tag.`mappd.workflow_id` = "wf-1"'
    assert body["locations"] == [
        {"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": "7"}}
    ]


@pytest.mark.parametrize(
    "answer",
    [
        httpx2.Response(500, text="down"),
        httpx2.Response(200, json={"traces": []}),
        httpx2.Response(200, json={}),
        httpx2.ConnectError("refused"),
    ],
)
def test_find_trace_returns_none_on_errors(answer: httpx2.Response | Exception) -> None:
    assert telemetry.find_trace(Settings(), "wf-1", transport=_mlflow(answer, [])) is None


def test_trace_filter_escapes_quotes() -> None:
    seen: list[httpx2.Request] = []
    ok = httpx2.Response(200, json={"traces": [{"trace_id": "tr-1"}]})
    telemetry.find_trace(Settings(), "o'brien", transport=_mlflow(ok, seen))
    telemetry.find_trace(Settings(), 'say "hi"', transport=_mlflow(ok, seen))
    filters = [json.loads(r.content)["filter"] for r in seen]
    assert filters == [
        'tag.`mappd.workflow_id` = "o\'brien"',
        "tag.`mappd.workflow_id` = 'say \"hi\"'",
    ]
    assert telemetry.find_trace(Settings(), "both ' and \"", transport=_mlflow(ok, seen)) is None
    assert len(seen) == 2, "an ID with both quotes is never sent"


def test_trace_page_urls() -> None:
    settings = Settings(mlflow_url="http://mlflow:5000", mlflow_experiment_id="7")
    assert (
        telemetry.trace_page(settings, "tr-1")
        == "http://mlflow:5000/#/experiments/7/traces?selectedEvaluationId=tr-1"
    )
    assert telemetry.trace_page(settings, None) == "http://mlflow:5000/#/experiments/7/traces"


def filtered() -> tuple[trace.Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(IdleTraceFilter(SimpleSpanProcessor(exporter)))
    return provider.get_tracer("t"), exporter


def test_an_idle_tick_is_never_exported() -> None:
    tracer, exporter = filtered()
    with tracer.start_as_current_span("mappd.mail_poll"):
        with tracer.start_as_current_span("fetch"):
            pass
        idle()
    assert exporter.get_finished_spans() == ()


def test_a_tick_that_did_something_is_exported_whole() -> None:
    tracer, exporter = filtered()
    with tracer.start_as_current_span("mappd.mail_poll") as root:
        with tracer.start_as_current_span("fetch"):
            pass
        held = exporter.get_finished_spans()
        assert held == (), "children wait for their root's verdict"
    late = tracer.start_span("process_ticket", context=trace.set_span_in_context(root))
    late.end()
    names = [s.name for s in exporter.get_finished_spans()]
    assert names == ["fetch", "mappd.mail_poll", "process_ticket"], "a late child follows"
