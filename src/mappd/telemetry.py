"""OpenTelemetry: one trace per workflow, from the HTTP request down to gateway spans.

Agent spans go to MLflow over OTLP. Calls to the gateway carry `traceparent`, so with
`clientSampling` on in agentgateway its spans (tokens, latency, provider) join the same trace.

The gateway client is an `httpx2.AsyncClient` with mappd's own tracing transport:
opentelemetry-instrumentation-httpx 0.66b1 crashes on httpx2 clients, and Pydantic AI
deprecates `httpx.AsyncClient`.
"""

import copy
import json
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx2
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from pydantic import BaseModel
from pydantic_ai import Agent, InstrumentationSettings

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from mappd.settings import Settings

GATEWAY_TIMEOUT = 120.0
WORKFLOW_TAG = "mlflow.traceTag.mappd.workflow_id"
TRACE_LOOKUP_SECONDS = 3.0
CLIENT_ERROR = 400  # client spans: 4xx and 5xx are errors (OTel HTTP conventions)
IDLE = "mappd.idle"


def _loads(value: object) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _stringify(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _chat_tools(raw: object) -> str | None:
    """Normalize OTel GenAI `gen_ai.tool.definitions` into MLflow's OpenAI tool shape."""
    defs = _loads(raw)
    if not isinstance(defs, list):
        return None
    tools: list[dict[str, Any]] = []
    for item in defs:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("function"), dict):
            tools.append(item)
        elif isinstance(item.get("name"), str):
            fn: dict[str, Any] = {"name": item["name"]}
            if isinstance(item.get("description"), str):
                fn["description"] = item["description"]
            if isinstance(item.get("parameters"), dict):
                fn["parameters"] = item["parameters"]
            tools.append({"type": "function", "function": fn})
    return json.dumps(tools, ensure_ascii=False) if tools else None


def _message_from_parts(role: str, parts: list[Any]) -> list[dict[str, Any]]:
    """Turn one OTel GenAI message's `parts` into MLflow chat message entries."""
    out: list[dict[str, Any]] = []
    texts: list[str] = []
    thinkings: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for part in (p for p in parts if isinstance(p, dict)):
        kind, content = part.get("type"), part.get("content")
        if kind == "text" and isinstance(content, str) and content.strip():
            texts.append(content)
        elif kind == "thinking" and isinstance(content, str) and content.strip():
            thinkings.append(content)
        elif kind == "tool_call" and isinstance(part.get("name"), str):
            args = part.get("arguments")
            tool_calls.append(
                {
                    "id": str(part.get("id") or part["name"]),
                    "function": {
                        "name": part["name"],
                        "arguments": _stringify(args if args is not None else {}),
                    },
                }
            )
        elif kind == "tool_call_response":
            result = part.get("result", part.get("response", ""))
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": str(part.get("id") or ""),
                    "name": str(part.get("name") or ""),
                    "content": _stringify(result),
                }
            )
    if texts or thinkings or tool_calls:
        entry: dict[str, Any] = {
            "role": role,
            "content": "\n\n".join(texts) if texts else None,
        }
        if thinkings:
            entry["reasoning"] = "\n\n".join(thinkings)
        if tool_calls:
            entry["tool_calls"] = tool_calls
        out.append(entry)
    return out


def _chat_messages(
    instructions_raw: object, messages_raw: Sequence[object]
) -> list[dict[str, Any]]:
    """Convert OTel GenAI message arrays into MLflow's `mlflow.chat.messages` format."""
    out: list[dict[str, Any]] = []
    instructions = _loads(instructions_raw)
    if isinstance(instructions, list):
        sys_parts = [
            p["content"]
            for p in instructions
            if isinstance(p, dict) and isinstance(p.get("content"), str) and p["content"].strip()
        ]
        if sys_parts:
            out.append({"role": "system", "content": "\n\n".join(sys_parts)})
    for raw in messages_raw:
        msgs = _loads(raw)
        if not isinstance(msgs, list):
            continue
        for msg in (m for m in msgs if isinstance(m, dict) and isinstance(m.get("parts"), list)):
            out.extend(_message_from_parts(str(msg.get("role") or "user"), msg["parts"]))
    return out


def _enrich_tool_span(attrs: dict[str, Any]) -> None:
    """Type a `execute_tool` span TOOL for MLflow's Tools overview; args/result become IO."""
    if "mlflow.spanType" not in attrs:
        attrs["mlflow.spanType"] = "TOOL"
    if "mlflow.spanInputs" not in attrs and "gen_ai.tool.call.arguments" in attrs:
        attrs["mlflow.spanInputs"] = _stringify(_loads(attrs["gen_ai.tool.call.arguments"]))
    if "mlflow.spanOutputs" not in attrs and "gen_ai.tool.call.result" in attrs:
        attrs["mlflow.spanOutputs"] = _stringify(_loads(attrs["gen_ai.tool.call.result"]))


def _enrich_dbos_span(attrs: dict[str, Any], *, root: bool) -> None:
    """Type DBOS workflow and step spans; tag a root workflow span with its workflow ID.

    MLflow keeps trace tags from the root span only, so the dashboard finds a trace by its
    root workflow's ID.
    """
    workflow = attrs["operationType"] == "workflow"
    attrs.setdefault("mlflow.spanType", "WORKFLOW" if workflow else "TASK")
    if workflow and root and "operationUUID" in attrs:
        attrs.setdefault(WORKFLOW_TAG, attrs["operationUUID"])


def _enrich_attributes(span: ReadableSpan) -> dict[str, Any]:
    """Translate Pydantic AI / DBOS span attributes into the keys MLflow's UI reads."""
    attrs: dict[str, Any] = dict(span.attributes or {})
    op = attrs.get("gen_ai.operation.name")
    if op == "chat":
        if (
            "mlflow.chat.tools" not in attrs
            and "gen_ai.tool.definitions" in attrs
            and (tools := _chat_tools(attrs["gen_ai.tool.definitions"]))
        ):
            attrs["mlflow.chat.tools"] = tools
        if "mlflow.chat.messages" not in attrs:
            msgs = _chat_messages(
                attrs.get("gen_ai.system_instructions"),
                (attrs.get("gen_ai.input.messages"), attrs.get("gen_ai.output.messages")),
            )
            if msgs:
                attrs["mlflow.chat.messages"] = json.dumps(msgs, ensure_ascii=False)
    elif op == "invoke_agent":
        msgs = _chat_messages(
            attrs.get("gen_ai.system_instructions"),
            (attrs.get("pydantic_ai.all_messages"),),
        )
        if msgs and "mlflow.chat.messages" not in attrs:
            attrs["mlflow.chat.messages"] = json.dumps(msgs, ensure_ascii=False)
        if "mlflow.spanInputs" not in attrs:
            user = next(
                (m["content"] for m in msgs if m.get("role") == "user" and m.get("content")),
                None,
            )
            if user:
                attrs["mlflow.spanInputs"] = json.dumps({"prompt": user}, ensure_ascii=False)
        if "mlflow.spanOutputs" not in attrs and "final_result" in attrs:
            attrs["mlflow.spanOutputs"] = _stringify(_loads(attrs["final_result"]))
    elif op == "execute_tool":
        _enrich_tool_span(attrs)
    elif attrs.get("operationType") in ("workflow", "step"):
        _enrich_dbos_span(attrs, root=span.parent is None)
    # Per-call conversation UUIDs pollute MLflow's Session column; session.id owns it.
    attrs.pop("gen_ai.conversation.id", None)
    return attrs


class MlflowSpanExporter(SpanExporter):
    """Wraps a `SpanExporter`, adding the `mlflow.*` attributes MLflow's UI expects."""

    def __init__(self, inner: SpanExporter) -> None:
        """Forward enriched spans to `inner`."""
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Enrich each span's attributes, then delegate to `inner`."""
        enriched: list[ReadableSpan] = []
        for span in spans:
            clone = copy.copy(span)
            clone._attributes = _enrich_attributes(span)
            enriched.append(clone)
        return self._inner.export(enriched)

    def shutdown(self) -> None:
        """Shut down `inner`."""
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Flush `inner`."""
        return self._inner.force_flush(timeout_millis)


def annotate_trace(
    *,
    name: str | None = None,
    inputs: object = None,
    outputs: object = None,
    user: str | None = None,
    session: str | None = None,
    span_type: str | None = None,
    tags: Mapping[str, str] | None = None,
) -> None:
    """Set MLflow trace-table and summary attributes on the current span."""
    span = trace.get_current_span()
    if not span.is_recording():
        return
    if name is not None:
        span.set_attribute("mlflow.traceTag.mlflow.traceName", name)
    if inputs is not None:
        span.set_attribute("mlflow.spanInputs", _stringify(inputs))
    if outputs is not None:
        span.set_attribute("mlflow.spanOutputs", _stringify(outputs))
    if user:
        span.set_attribute("user.id", user)
    if session:
        span.set_attribute("session.id", session)
    if span_type:
        span.set_attribute("mlflow.spanType", span_type)
    for key, value in (tags or {}).items():
        span.set_attribute(f"mlflow.traceTag.{key}", value)


@dataclass
class TraceNote:
    """Mutable holder for what a `traced` block records on exit."""

    outputs: object = None


@contextmanager
def traced(
    name: str,
    *,
    inputs: object = None,
    user: str | None = None,
    session: str | None = None,
    span_type: str | None = None,
    tags: Mapping[str, str] | None = None,
) -> Iterator[TraceNote]:
    """Annotate the current span on entry; `note.outputs` is recorded on clean exit.

    A workflow wraps its body in this block so the trace-name/inputs and the final outputs
    bracket the steps instead of repeating `annotate_trace` calls at both ends.
    """
    annotate_trace(
        name=name, inputs=inputs, user=user, session=session, span_type=span_type, tags=tags
    )
    note = TraceNote()
    yield note
    if note.outputs is not None:
        annotate_trace(outputs=note.outputs)


def _tag_literal(value: str) -> str | None:
    """`value` quoted for MLflow's filter grammar, which has no escapes: None if impossible."""
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    return None


def find_trace(
    settings: Settings, workflow_id: str, *, transport: httpx2.BaseTransport | None = None
) -> str | None:
    """The MLflow trace whose root workflow is `workflow_id`; None if absent or unreachable."""
    literal = _tag_literal(workflow_id)
    if literal is None:
        return None
    body = {
        "locations": [
            {
                "type": "MLFLOW_EXPERIMENT",
                "mlflow_experiment": {"experiment_id": settings.mlflow_experiment_id},
            }
        ],
        "filter": f"tag.`mappd.workflow_id` = {literal}",
        "max_results": 1,
    }
    url = f"{settings.mlflow_url}/api/3.0/mlflow/traces/search"
    try:
        with httpx2.Client(transport=transport, timeout=TRACE_LOOKUP_SECONDS) as client:
            response = client.post(url, json=body)
    except httpx2.HTTPError:
        return None
    if response.status_code != httpx2.codes.OK:
        return None
    traces = response.json().get("traces") or []
    return str(traces[0]["trace_id"]) if traces else None


def trace_page(settings: Settings, trace_id: str | None) -> str:
    """MLflow's page for one trace, or the experiment's trace list when there is none."""
    page = f"{settings.mlflow_url}/#/experiments/{settings.mlflow_experiment_id}/traces"
    return f"{page}?selectedEvaluationId={trace_id}" if trace_id else page


def idle() -> None:
    """Mark the running workflow's trace as one that did nothing, so it is never exported.

    For scheduled ticks that found no mail, no update and nothing due: idle, they would be
    nearly every trace in MLflow.
    """
    trace.get_current_span().set_attribute(IDLE, True)


class IdleTraceFilter(SpanProcessor):
    """Holds a trace's spans until its local root ends, then drops the trace if it was idle.

    Spans arriving after the root (a workflow the trace started) follow the root's verdict.
    A trace whose root never ends here is let through once `MAX_HELD` spans pile up.
    """

    MAX_HELD = 512
    REMEMBERED = 4096  # verdicts kept for late spans

    def __init__(self, inner: SpanProcessor) -> None:
        """Pass kept spans on to `inner`."""
        self._inner = inner
        self._held: dict[int, list[ReadableSpan]] = {}
        self._verdicts: OrderedDict[int, bool] = OrderedDict()  # trace id -> keep
        self._lock = threading.Lock()

    def on_start(self, span: Span, parent_context: Any = None) -> None:
        """Start as `inner` does."""
        self._inner.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        """Hold the span, or decide its trace when it is the local root."""
        context = span.get_span_context()
        trace_id = context.trace_id if context is not None else 0
        root = span.parent is None or span.parent.is_remote
        with self._lock:
            if trace_id in self._verdicts:
                keep, batch = self._verdicts[trace_id], [span]
            else:
                held = self._held.setdefault(trace_id, [])
                held.append(span)
                if not root and len(held) < self.MAX_HELD:
                    return
                batch = self._held.pop(trace_id)
                keep = not root or not any((s.attributes or {}).get(IDLE) for s in batch)
                if root:
                    self._verdicts[trace_id] = keep
                    while len(self._verdicts) > self.REMEMBERED:
                        self._verdicts.popitem(last=False)
        if keep:
            for s in batch:
                self._inner.on_end(s)

    def shutdown(self) -> None:
        """Let held spans through, then shut `inner` down."""
        with self._lock:
            held = [s for spans in self._held.values() for s in spans]
            self._held.clear()
        for s in held:
            self._inner.on_end(s)
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Flush `inner` (held spans wait for their root)."""
        return self._inner.force_flush(timeout_millis)


def mlflow_exporter(settings: Settings) -> OTLPSpanExporter:
    """OTLP/HTTP exporter to MLflow; MLflow requires the experiment as a header."""
    return OTLPSpanExporter(
        endpoint=f"{settings.mlflow_url}/v1/traces",
        headers={"x-mlflow-experiment-id": settings.mlflow_experiment_id},
    )


def setup(settings: Settings, exporter: SpanExporter | None = None) -> TracerProvider:
    """Install the global tracer provider and instrument every Pydantic AI agent."""
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": settings.app_name, "service.version": settings.app_version}
        )
    )
    wrapped = MlflowSpanExporter(exporter or mlflow_exporter(settings))
    provider.add_span_processor(IdleTraceFilter(BatchSpanProcessor(wrapped)))
    trace.set_tracer_provider(provider)
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider))
    return provider


@contextmanager
def _client_span(tracer: trace.Tracer, request: httpx2.Request) -> Iterator[trace.Span]:
    """A CLIENT span "METHOD /path" around one request; injects `traceparent` into it."""
    with tracer.start_as_current_span(
        f"{request.method} {request.url.path}",
        kind=trace.SpanKind.CLIENT,
        attributes={"http.request.method": request.method, "url.full": str(request.url)},
    ) as span:
        propagate.inject(request.headers)
        yield span


def _record_response(span: trace.Span, response: httpx2.Response) -> None:
    """Record the status on `span`; 4xx and 5xx are errors (OTel HTTP conventions)."""
    span.set_attribute("http.response.status_code", response.status_code)
    if response.status_code >= CLIENT_ERROR:
        span.set_status(trace.StatusCode.ERROR)
        span.set_attribute("error.type", str(response.status_code))


class TracedTransport(httpx2.AsyncBaseTransport):
    """A client span per request, with `traceparent` injected; wraps another transport."""

    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport | None = None,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        """Wrap `inner` (default: a plain HTTP transport)."""
        self._inner = inner or httpx2.AsyncHTTPTransport()
        self._tracer = trace.get_tracer("mappd.gateway", tracer_provider=tracer_provider)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Send `request` inside a client span."""
        with _client_span(self._tracer, request) as span:
            response = await self._inner.handle_async_request(request)
            _record_response(span, response)
            return response

    async def aclose(self) -> None:
        """Close the wrapped transport."""
        await self._inner.aclose()


class TracedSyncTransport(httpx2.BaseTransport):
    """The sync counterpart of `TracedTransport`, for sync callers such as DBOS steps."""

    def __init__(
        self,
        inner: httpx2.BaseTransport | None = None,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        """Wrap `inner` (default: a plain HTTP transport)."""
        self._inner = inner or httpx2.HTTPTransport()
        self._tracer = trace.get_tracer("mappd.gateway", tracer_provider=tracer_provider)

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        """Send `request` inside a client span."""
        with _client_span(self._tracer, request) as span:
            response = self._inner.handle_request(request)
            _record_response(span, response)
            return response

    def close(self) -> None:
        """Close the wrapped transport."""
        self._inner.close()


def traced_client(
    timeout: float = GATEWAY_TIMEOUT,
    transport: httpx2.BaseTransport | None = None,
    tracer_provider: TracerProvider | None = None,
) -> httpx2.Client:
    """A sync HTTP client whose requests join the current trace."""
    return httpx2.Client(
        transport=TracedSyncTransport(transport, tracer_provider=tracer_provider),
        timeout=timeout,
    )
