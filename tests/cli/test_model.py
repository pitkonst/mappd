import json

import pytest
from fake_gateway import MODELS, Gateway
from opentelemetry.sdk.trace import TracerProvider
from typer.testing import CliRunner

from mappd import telemetry
from mappd.cli import app

runner = CliRunner()


@pytest.fixture
def gateway_env(fake_gateway: Gateway, monkeypatch: pytest.MonkeyPatch) -> Gateway:
    monkeypatch.setenv("MAPPD_GATEWAY_URL", fake_gateway.url)
    # Keep the global tracer provider untouched; the CLI would install one.
    monkeypatch.setattr(telemetry, "setup", lambda _settings: TracerProvider())
    return fake_gateway


def test_model_list_json(gateway_env: Gateway) -> None:
    r = runner.invoke(app, ["model", "list", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.stdout) == {"models": sorted(MODELS)}


def test_model_list_text_is_one_name_per_line(gateway_env: Gateway) -> None:
    r = runner.invoke(app, ["model", "list"])
    assert r.stdout.splitlines() == sorted(MODELS)


def test_model_list_empty_says_no_models_routed(
    gateway_env: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    del gateway_env
    monkeypatch.setattr("mappd.cli.model.gateway.list_models", lambda _s: [])
    r = runner.invoke(app, ["model", "list"])
    assert r.stdout.strip() == "no models routed"


def test_model_list_with_gateway_down_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPPD_GATEWAY_URL", "http://127.0.0.1:9/v1")
    r = runner.invoke(app, ["model", "list"])
    assert r.exit_code == 1
    assert r.stderr.startswith("error: ")


def test_model_try_json(gateway_env: Gateway) -> None:
    r = runner.invoke(app, ["model", "try", "fast", "--prompt", "ping", "--json"])
    assert r.exit_code == 0
    out = json.loads(r.stdout)
    assert out["output"] == "pong"
    assert out["input_tokens"] == 3


def test_model_try_unknown_model_exits_1(gateway_env: Gateway) -> None:
    r = runner.invoke(app, ["model", "try", "huge", "--prompt", "ping"])
    assert r.exit_code == 1


def test_model_try_requires_a_prompt(gateway_env: Gateway) -> None:
    assert runner.invoke(app, ["model", "try", "fast"]).exit_code == 2
