import asyncio

import httpx2
import pytest
from fake_gateway import MODELS, Gateway
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel

from mappd import gateway
from mappd.gateway import GatewayError, list_models, probe, try_model
from mappd.settings import Settings


def test_model_and_client_use_default_settings_or_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAPPD_GATEWAY_URL", "http://env-gw:4000/v1")
    default_model = gateway.model("fast")
    assert isinstance(default_model, OpenAIChatModel)
    assert default_model.model_name == "fast"

    custom = Settings(gateway_url="http://custom-gw:4000/v1")
    custom_client = gateway.client(custom)
    custom_model = gateway.model("smart", custom, client=custom_client)
    assert isinstance(custom_model, OpenAIChatModel)
    assert custom_model.model_name == "smart"


def test_gateway_model_calls_the_gateway_with_a_placeholder_key() -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "fast",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )

    settings = Settings(gateway_url="http://gateway:4000/v1")
    http = gateway.client(
        settings, transport=httpx2.MockTransport(handler), tracer_provider=provider
    )
    agent = Agent(gateway.model("fast", settings, client=http))
    assert asyncio.run(agent.run("ping")).output == "pong"
    [request] = seen
    assert str(request.url) == "http://gateway:4000/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {gateway.GATEWAY_API_KEY}"
    assert "traceparent" in request.headers


def test_gateway_embed_calls_the_gateway_embeddings_endpoint() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"data": [{"embedding": [0.25, 0.75]}]})

    settings = Settings(gateway_url="http://gateway:4000/v1")
    http = gateway.client(settings, transport=httpx2.MockTransport(handler))
    vector = asyncio.run(
        gateway.embed("sensor offline", dimensions=2, settings=settings, client=http)
    )
    assert vector == [0.25, 0.75]
    [request] = seen
    assert str(request.url) == "http://gateway:4000/v1/embeddings"


def test_list_models_returns_sorted_names(fake_gateway: Gateway) -> None:
    assert list_models(Settings(gateway_url=fake_gateway.url)) == sorted(MODELS)


@pytest.mark.parametrize("url", ["http://127.0.0.1:9/v1", "WRONG"])
def test_unreachable_gateway_is_a_gateway_error(url: str) -> None:
    with pytest.raises(GatewayError):
        list_models(Settings(gateway_url=url))


def test_wrong_path_is_a_gateway_error(fake_gateway: Gateway) -> None:
    with pytest.raises(GatewayError, match="404"):
        list_models(Settings(gateway_url=f"{fake_gateway.root}/nope"))


def test_try_model_returns_output_and_usage(fake_gateway: Gateway) -> None:
    result = asyncio.run(try_model(Settings(gateway_url=fake_gateway.url), "fast", "ping"))
    assert result.model == "fast"
    assert result.output == "pong"
    assert (result.input_tokens, result.output_tokens) == (3, 1)
    assert len(result.trace_id) == 32
    [request] = fake_gateway.requests
    assert request["body"]["model"] == "fast"


def test_try_unknown_model_is_a_gateway_error(fake_gateway: Gateway) -> None:
    with pytest.raises(GatewayError, match="huge"):
        asyncio.run(try_model(Settings(gateway_url=fake_gateway.url), "huge", "ping"))


def test_probe_makes_one_tiny_call(fake_gateway: Gateway) -> None:
    probe("fast", Settings(gateway_url=fake_gateway.url))
    [request] = fake_gateway.requests
    assert (request["body"]["model"], request["body"]["max_tokens"]) == ("fast", 1)


def test_probe_reports_the_providers_answer(fake_gateway: Gateway) -> None:
    with pytest.raises(GatewayError, match=r"huge: 404 .*unknown model"):
        probe("huge", Settings(gateway_url=fake_gateway.url))
