import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mappd import gateway
from mappd.data import synth
from mappd.data.cases import CasePlan, Message, Slot, read_cases
from mappd.data.hook import SynthSpec
from mappd.settings import Settings
from processes.support.mocks.mock_llm import check_json, synth_text

REPO = Path(__file__).parents[2]


def mock_gateway(fail_after: int | None = None) -> httpx2.Client:
    """The mock model behind a fake gateway: routes `synth` and `check` like gateway.mocks.yaml."""
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        body: dict[str, Any] = json.loads(request.content)
        calls["n"] += 1
        if fail_after is not None and calls["n"] > fail_after:
            return httpx2.Response(503)
        produce = synth_text if body["model"] == "synth" else check_json
        return httpx2.Response(
            200, json={"choices": [{"message": {"content": produce(body["messages"])}}]}
        )

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def settings() -> Settings:
    return Settings(
        processes_dir=REPO / "processes", world_path=REPO / "world.yaml", gateway_url="http://gw/v1"
    )


def test_dry_run_plans_the_mix_without_model_calls(tmp_path: Path) -> None:
    report = synth.run(
        settings(),
        "support",
        seed=1,
        n=20,
        out_dir=tmp_path,
        cache_dir=tmp_path / "c",
        dry_run=True,
        check_models=False,
    )
    assert report.planned == {"reply": 10, "clarify": 4, "escalate": 6}
    assert report.path is None
    assert not (tmp_path / "synth.jsonl").exists()


def test_run_writes_round_tripped_cases(tmp_path: Path) -> None:
    report = synth.run(
        settings(),
        "support",
        seed=42,
        n=60,
        out_dir=tmp_path,
        cache_dir=tmp_path / "c",
        client=mock_gateway(),
        check_models=False,
    )
    cases = read_cases(tmp_path / "synth.jsonl")
    assert len(cases) == sum(report.kept.values())
    assert sum(report.dropped.values()) > 0, "the mock checker misses some traps"
    assert not report.failed
    first = cases[0]
    assert first.id.startswith("s42-")
    assert first.input["external_id"] == first.id
    assert set(first.meta) == {"slot", "trap", "persona", "seed"}


def test_progress_reports_each_round_trip(tmp_path: Path) -> None:
    calls: list[tuple[int, int]] = []
    synth.run(
        settings(),
        "support",
        seed=2,
        n=8,
        out_dir=tmp_path,
        cache_dir=tmp_path / "c",
        client=mock_gateway(),
        check_models=False,
        progress=lambda done, total: calls.append((done, total)),
    )
    assert calls[0] == (0, 8)
    assert calls[-1] == (8, 8)
    assert [done for done, _ in calls] == list(range(9))


def test_second_run_is_served_from_the_cache(tmp_path: Path) -> None:
    args: dict[str, Any] = {
        "seed": 7,
        "n": 10,
        "out_dir": tmp_path,
        "cache_dir": tmp_path / "c",
        "check_models": False,
    }
    synth.run(settings(), "support", client=mock_gateway(), **args)
    report = synth.run(
        settings(), "support", client=mock_gateway(fail_after=2), **args
    )  # the two probes
    assert report.cache_misses == 0


def test_gateway_failure_reports_how_far_it_got(tmp_path: Path) -> None:
    with pytest.raises(synth.SynthError) as exc:
        synth.run(
            settings(),
            "support",
            seed=3,
            n=10,
            out_dir=tmp_path,
            cache_dir=tmp_path / "c",
            client=mock_gateway(fail_after=8),
            check_models=False,
            concurrency=1,  # the count of written cases depends on call order
            backoff=0,
        )
    assert exc.value.written == 3


def test_unrouted_models_are_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(synth, "list_models", lambda _s: ["fast", "smart", "embed"])
    with pytest.raises(synth.ConfigError, match="synth, check"):
        synth.run(settings(), "support", seed=1, n=5, out_dir=tmp_path, cache_dir=tmp_path / "c")


def test_more_than_half_dropped_fails_the_run(tmp_path: Path) -> None:
    def always_escalate(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        text = (
            synth_text(body["messages"])
            if body["model"] == "synth"
            else ('{"route": "technical", "outcome": "escalate", "language": "en"}')
        )
        return httpx2.Response(200, json={"choices": [{"message": {"content": text}}]})

    report = synth.run(
        settings(),
        "support",
        seed=5,
        n=20,
        out_dir=tmp_path,
        cache_dir=tmp_path / "c",
        client=httpx2.Client(transport=httpx2.MockTransport(always_escalate)),
        check_models=False,
    )
    assert report.failed
    assert "outcome" in report.disagreements


def test_gateway_down_at_the_start_is_a_synth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(_settings: Settings) -> list[str]:
        raise gateway.GatewayError("http://gw/v1/models: Connection refused")

    monkeypatch.setattr(synth, "list_models", down)
    with pytest.raises(synth.SynthError, match="Connection refused") as exc:
        synth.run(settings(), "support", seed=1, n=5, out_dir=tmp_path, cache_dir=tmp_path / "c")
    assert exc.value.written == 0


def test_custom_domain_synth_runs_without_world_yaml_or_customer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_prompts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body: dict[str, Any] = json.loads(request.content)
        if body["model"] == "synth":
            seen_prompts.append(json.dumps(body["messages"]))
            return httpx2.Response(
                200,
                json={"choices": [{"message": {"content": "Subject: Claim\n\nBroken screen"}}]},
            )
        return httpx2.Response(
            200, json={"choices": [{"message": {"content": '{"decision": "approve"}'}}]}
        )

    def plan_case(slot: Slot, _rng: object) -> CasePlan:
        return CasePlan(
            id="",
            slot=slot,
            facts=["The screen cracked during shipping."],
            labels={"decision": slot.kind},
        )

    def to_input(plan: CasePlan, message: Message) -> dict[str, Any]:
        return {"id": plan.id, "text": message.body}

    spec = SynthSpec(
        mix={"approve": 1.0},
        traps={},
        trap_rate=0.0,
        roundtrip_fields=("decision",),
        strict_fields=("decision",),
        writer_instructions="Write an insurance claim.",
        check_instructions="Return decision.",
        plan_case=plan_case,
        to_input=to_input,
        output_dir=tmp_path / "out",
    )
    monkeypatch.setattr(synth, "load_hook", lambda _dir, _name: spec)

    cfg = Settings(
        processes_dir=tmp_path / "procs",
        world_path=tmp_path / "missing_world.yaml",
        gateway_url="http://gw/v1",
    )
    report = synth.run(
        cfg,
        "claims",
        seed=1,
        n=2,
        cache_dir=tmp_path / "cache",
        client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        check_models=False,
    )
    assert report.kept == {"approve": 2}
    assert any("Write an insurance claim." in p for p in seen_prompts)
    cases = read_cases(tmp_path / "out" / "synth.jsonl")
    assert len(cases) == 2
    assert cases[0].meta["persona"] is None


def test_cases_run_concurrently_and_land_in_plan_order(tmp_path: Path) -> None:
    lock = threading.Lock()
    state = {"in_flight": 0, "max": 0}
    inner = mock_gateway()

    def handler(request: httpx2.Request) -> httpx2.Response:
        with lock:
            state["in_flight"] += 1
            state["max"] = max(state["max"], state["in_flight"])
        time.sleep(0.02)
        try:
            return inner.post(str(request.url), content=request.content)
        finally:
            with lock:
                state["in_flight"] -= 1

    args: dict[str, Any] = {"seed": 5, "n": 16, "cache_dir": tmp_path / "c", "check_models": False}
    synth.run(
        settings(),
        "support",
        out_dir=tmp_path / "par",
        concurrency=4,
        client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        **args,
    )
    assert state["max"] >= 2, "calls overlap"
    synth.run(
        settings(),
        "support",
        out_dir=tmp_path / "seq",
        concurrency=1,
        client=mock_gateway(),
        **args,
    )
    par = [c.id for c in read_cases(tmp_path / "par" / "synth.jsonl")]
    seq = [c.id for c in read_cases(tmp_path / "seq" / "synth.jsonl")]
    assert par == seq


def test_an_interrupted_run_stops_spending(tmp_path: Path) -> None:
    """Ctrl-C or any other error cancels the queued cases instead of running them all."""
    lock = threading.Lock()
    calls = {"n": 0}
    inner = mock_gateway()

    def handler(request: httpx2.Request) -> httpx2.Response:
        with lock:
            calls["n"] += 1
            n = calls["n"]
        if n == 3:
            raise KeyboardInterrupt
        time.sleep(0.01)
        return inner.post(str(request.url), content=request.content)

    with pytest.raises(KeyboardInterrupt):
        synth.run(
            settings(),
            "support",
            seed=9,
            n=60,
            out_dir=tmp_path,
            cache_dir=tmp_path / "c",
            client=httpx2.Client(transport=httpx2.MockTransport(handler)),
            check_models=False,
            concurrency=2,
            backoff=0,
        )
    assert calls["n"] < 20, f"{calls['n']} calls after the interrupt"
