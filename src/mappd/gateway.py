"""The model gateway: clients, Pydantic AI models, routed model discovery, and CLI test calls.

All model traffic goes through agentgateway (AGENTS.md); provider keys live only there.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx2
from openai import APIError
from opentelemetry import trace
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from mappd import telemetry
from mappd.settings import Settings

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

TIMEOUT = 5.0
GATEWAY_API_KEY = "unused-keys-live-in-the-gateway"  # provider keys live only in the gateway
GATEWAY_TIMEOUT = 120.0
_DEFAULT_CLIENT: httpx2.AsyncClient | None = None


class GatewayError(RuntimeError):
    """The gateway is unreachable or answered with an error."""


@dataclass(frozen=True)
class TryResult:
    """One completion through the gateway."""

    model: str
    output: str
    input_tokens: int
    output_tokens: int
    trace_id: str


def client(
    settings: Settings | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    tracer_provider: TracerProvider | None = None,
) -> httpx2.AsyncClient:
    """HTTP client for the gateway whose requests join the current trace."""
    del settings  # reserved for per-deployment client options
    return httpx2.AsyncClient(
        transport=telemetry.TracedTransport(transport, tracer_provider=tracer_provider),
        timeout=GATEWAY_TIMEOUT,
    )


def _default_client() -> httpx2.AsyncClient:
    global _DEFAULT_CLIENT  # noqa: PLW0603 - one shared gateway client per process
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = client()
    return _DEFAULT_CLIENT


def model(
    name: str,
    settings: Settings | None = None,
    *,
    client: httpx2.AsyncClient | None = None,
) -> OpenAIChatModel:
    """A Pydantic AI model routed through the gateway; `name` is a gateway model name."""
    resolved = settings or Settings.from_env()
    http = client or _default_client()
    provider = OpenAIProvider(
        base_url=resolved.gateway_url,
        api_key=GATEWAY_API_KEY,
        http_client=http,
    )
    return OpenAIChatModel(name, provider=provider)


async def embed(
    text: str,
    *,
    dimensions: int | None = None,
    model_name: str = "embed",
    settings: Settings | None = None,
    client: httpx2.AsyncClient | None = None,
) -> list[float]:
    """An embedding vector through the gateway (`/embeddings`)."""
    resolved = settings or Settings.from_env()
    http = client or _default_client()
    payload: dict[str, Any] = {"model": model_name, "input": [text]}
    if dimensions is not None:
        payload["dimensions"] = dimensions
    response = await http.post(f"{resolved.gateway_url}/embeddings", json=payload)
    response.raise_for_status()
    vector: list[float] = response.json()["data"][0]["embedding"]
    return vector


def list_models(settings: Settings | None = None) -> list[str]:
    """Names of the models the gateway routes (OpenAI `GET /models`), sorted."""
    resolved = settings or Settings.from_env()
    url = f"{resolved.gateway_url}/models"
    try:
        response = httpx2.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        return sorted(str(m["id"]) for m in response.json()["data"])
    except (httpx2.HTTPError, KeyError, TypeError, ValueError) as e:
        raise GatewayError(f"{url}: {e}") from e


def probe(model_name: str, settings: Settings | None = None) -> None:
    """One 1-token chat call to `model_name`: proves the gateway reaches its provider.

    Listing models only reads the gateway's config. A missing provider key fails here
    instead, before an agent's first real call.

    Raises:
        GatewayError: the call did not succeed (the provider's answer is in the message).
    """
    resolved = settings or Settings.from_env()
    url = f"{resolved.gateway_url}/chat/completions"
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    try:
        response = httpx2.post(url, json=payload, timeout=TIMEOUT)
    except httpx2.HTTPError as e:
        raise GatewayError(f"{model_name}: {e}") from e
    if response.is_error:
        raise GatewayError(f"{model_name}: {response.status_code} {response.text[:200].strip()}")


async def try_model(
    settings: Settings,
    model_name: str,
    prompt: str,
    http_client: httpx2.AsyncClient | None = None,
) -> TryResult:
    """Send `prompt` to `model_name` through the gateway inside its own trace."""
    http = http_client or client(settings)
    agent = Agent(model(model_name, settings, client=http), name="mappd-model-try")
    tracer = trace.get_tracer("mappd.cli")
    try:
        with tracer.start_as_current_span(f"mappd model try {model_name}") as span:
            result = await agent.run(prompt)
            trace_id = format(span.get_span_context().trace_id, "032x")
    except (ModelAPIError, APIError, httpx2.HTTPError) as e:
        raise GatewayError(f"{model_name}: {e}") from e
    finally:
        if http_client is None:
            await http.aclose()
    usage = result.usage
    return TryResult(
        model=model_name,
        output=str(result.output),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        trace_id=trace_id,
    )
