from typing import TYPE_CHECKING

import httpx2
import pytest

from mappd.data.cache import Cache
from mappd.data.llm import Gateway, GatewayError, gateway_defaults
from mappd.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

MESSAGES = [{"role": "user", "content": "hi"}]


def gateway(tmp_path: Path, handler: Callable[[httpx2.Request], httpx2.Response]) -> Gateway:
    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    settings = Settings(gateway_url="http://gw/v1", gateway_config=tmp_path / "no-gateway.yaml")
    return Gateway(settings, Cache(tmp_path), client=client, backoff=0)


def reply(content: str) -> httpx2.Response:
    return httpx2.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_complete_calls_the_gateway_once_per_key(tmp_path: Path) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return reply("pong")

    gw = gateway(tmp_path, handler)
    assert gw.complete("synth", MESSAGES, 1) == "pong"
    assert gw.complete("synth", MESSAGES, 1) == "pong"
    assert len(calls) == 2, "one probe for the backend, one completion"
    assert str(calls[0].url) == "http://gw/v1/chat/completions"


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(503),
        httpx2.Response(200, json={"choices": []}),
        httpx2.Response(200, text="nope"),
    ],
)
def test_gateway_failures_raise(tmp_path: Path, response: httpx2.Response) -> None:
    gw = gateway(tmp_path, lambda _: response)
    with pytest.raises(GatewayError):
        gw.complete("synth", MESSAGES, 1)


def test_another_backend_does_not_get_cached_answers(tmp_path: Path) -> None:
    def backend(name: str, content: str) -> Callable[[httpx2.Request], httpx2.Response]:
        def handler(_: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200, json={"model": name, "choices": [{"message": {"content": content}}]}
            )

        return handler

    assert (
        gateway(tmp_path, backend("mock", "mock text")).complete("synth", MESSAGES, 1)
        == "mock text"
    )
    real = gateway(tmp_path, backend("gpt-real-2026", "real text"))
    assert real.complete("synth", MESSAGES, 1) == "real text"


def probe_reply(model: str, provider: str | None) -> httpx2.Response:
    body: dict[str, object] = {"model": model, "choices": [{"message": {"content": "x"}}]}
    if provider:
        body["provider"] = provider
    return httpx2.Response(200, json=body)


def test_the_host_is_not_part_of_the_backend(tmp_path: Path) -> None:
    # OpenRouter picks a host per call; keying on it would make every rerun miss the cache.
    # Pin hosts in the gateway defaults instead: those are part of the key.
    a = gateway(tmp_path, lambda _r: probe_reply("qwen/q", "Alibaba"))
    b = gateway(tmp_path, lambda _r: probe_reply("qwen/q", "DeepInfra"))
    assert a.backend("synth") == b.backend("synth")


def test_backend_is_the_model_and_the_defaults_hash(tmp_path: Path) -> None:
    gw = gateway(tmp_path, lambda _r: probe_reply("mock-synth", None))
    assert gw.backend("synth") == "mock-synth|"


def test_gateway_defaults_hash_per_model(tmp_path: Path) -> None:
    config = tmp_path / "gateway.yaml"
    config.write_text(
        "llm:\n  models:\n"
        "  - name: synth\n    defaults: {reasoning: {enabled: false}}\n"
        "  - name: fast\n"
    )
    hashes = gateway_defaults(config)
    assert hashes["fast"] == ""
    assert len(hashes["synth"]) == 64
    config.write_text(
        "llm:\n  models:\n  - name: synth\n    defaults: {reasoning: {enabled: true}}\n"
    )
    assert gateway_defaults(config)["synth"] != hashes["synth"]
    assert gateway_defaults(tmp_path / "missing.yaml") == {}


def test_changing_a_default_misses_the_cache(tmp_path: Path) -> None:
    config = tmp_path / "gateway.yaml"
    config.write_text("llm:\n  models:\n  - name: synth\n    defaults: {a: 1}\n")
    settings = Settings(gateway_url="http://gw/v1", gateway_config=config)

    def client() -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(lambda _r: probe_reply("m", "h")))

    first = Gateway(settings, Cache(tmp_path), client=client())
    config.write_text("llm:\n  models:\n  - name: synth\n    defaults: {a: 2}\n")
    second = Gateway(settings, Cache(tmp_path), client=client())
    assert first.backend("synth") != second.backend("synth")


def test_transient_failures_are_retried(tmp_path: Path) -> None:
    answers = [httpx2.Response(503), httpx2.Response(429)]

    def handler(request: httpx2.Request) -> httpx2.Response:
        if b'"ping"' in request.content:
            return reply("probe")
        if not answers:
            return reply("pong")
        return answers.pop(0)

    settings = Settings(gateway_url="http://gw/v1", gateway_config=tmp_path / "no-gateway.yaml")
    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    gw = Gateway(settings, Cache(tmp_path), client=client, backoff=0)
    assert gw.complete("synth", MESSAGES, 1) == "pong"


def test_a_timeout_is_retried(tmp_path: Path) -> None:
    seen = {"timeouts": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if b'"ping"' not in request.content and seen["timeouts"] == 0:
            seen["timeouts"] += 1
            raise httpx2.ReadTimeout("slow provider", request=request)
        return reply("pong")

    settings = Settings(gateway_url="http://gw/v1", gateway_config=tmp_path / "no-gateway.yaml")
    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    gw = Gateway(settings, Cache(tmp_path), client=client, backoff=0)
    assert gw.complete("synth", MESSAGES, 1) == "pong"
    assert seen["timeouts"] == 1


def test_a_client_error_is_not_retried(tmp_path: Path) -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if b'"ping"' in request.content:
            return reply("probe")
        calls.append(1)
        return httpx2.Response(400)

    settings = Settings(gateway_url="http://gw/v1", gateway_config=tmp_path / "no-gateway.yaml")
    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    with pytest.raises(GatewayError):
        Gateway(settings, Cache(tmp_path), client=client, backoff=0).complete("synth", MESSAGES, 1)
    assert len(calls) == 1
