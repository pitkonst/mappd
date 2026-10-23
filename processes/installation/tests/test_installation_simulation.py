"""Simulated jobs: the technician follows a script, the case runs on the production engine."""

from typing import Any

import dbos_async
import httpx2
from pydantic_ai.models import Model

from mappd import gateway
from mappd.data.cases import Case
from mappd.eval.config import load_config
from processes.installation import process, simulator
from processes.installation.agents.interpreter import interpreter
from processes.installation.agents.simulator import INSTRUCTIONS
from processes.installation.agents.simulator import agent as simulator_agent
from processes.installation.evals import metrics, spec
from processes.installation.mocks import mock_llm
from processes.installation.sim import Scenario

SITE = "Riverside Offices"


def scenario(script: list[dict[str, str]], stock: dict[str, int] | None = None) -> Scenario:
    return Scenario.model_validate(
        {
            "job": {"external_id": "i-0001", "site": SITE, "skus": ["CTL-H400"]},
            "persona": "Short messages, a few words each.",
            "facts": {
                "stock": stock or {},
                "slots": [{"site": None, "at": "2026-10-23T09:00"}],
                "contacts": {
                    SITE: {"name": "Marta Novak", "phone": "+49 000 5550 1201", "access": "Desk."}
                },
            },
            "script": script,
        }
    )


HAPPY = [
    {"event": "arrived", "line": "arrived on site"},
    {"event": "access", "line": "access granted"},
    {"event": "equipment_found", "line": "found the boxes"},
    {"event": "installed", "photo": "late", "line": "installed the controller"},
    {"event": "tested", "photo": "yes", "line": "tested, all green"},
    {"event": "handed_over", "line": "handed over to the customer"},
]
MISSING = [
    {"event": "arrived", "line": "arrived on site"},
    {"event": "access", "line": "access granted"},
    {"event": "missing_parts", "line": "the controller is missing from the delivery"},
]


def mock_model() -> Model:
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=mock_llm.app))
    return gateway.model("smart", client=client)


def simulate(s: Scenario) -> dict[str, Any]:
    model = mock_model()
    with interpreter.override(model=model), simulator_agent.override(model=model):
        return dbos_async.run(simulator.run(s, resolver_model=model))


def test_the_simulator_prompt_starts_with_the_mock_marker() -> None:
    assert INSTRUCTIONS.startswith(mock_llm.SIMULATOR_MARK)


def test_a_happy_job_is_handed_over_after_a_late_photo() -> None:
    out = simulate(scenario(HAPPY))
    assert out["final_state"] == "handed_over"
    assert out["exception"] is None
    assert out["blocked_turn"] is None
    installs = [t for t in out["transcript"] if t["installer"].startswith("installed")]
    assert [t["photo"] is not None for t in installs] == [False, True], "the photo came later"
    assert "photo" in installs[0]["bot"].lower()
    assert len(out["states"]) == len(out["true_states"]) == len(out["transcript"])


def test_a_missing_part_in_stock_blocks_with_a_reservation_proposal() -> None:
    out = simulate(scenario(MISSING, stock={"CTL-H400": 2}))
    assert (out["final_state"], out["exception"]) == ("blocked", "missing_parts")
    assert (out["revealed_turn"], out["blocked_turn"]) == (2, 2)
    assert [p["tool"] for p in out["proposals"]] == ["reserve_parts"]
    assert [c["tool"] for c in out["tool_calls"]] == ["check_stock"]
    assert out["ticket"] is False
    expected = {
        "final_state": "blocked",
        "exception": "missing_parts",
        "proposals": [{"tool": "reserve_parts", "sku": "CTL-H400"}],
        "ticket": False,
    }
    c = Case(id="c", input={}, expected=expected, meta={})
    assert metrics.proposals_match(c, out) == 1.0
    assert metrics.timely_escalation(c, out) == 1.0
    assert metrics.no_premature_transition(c, out) == 1.0


def test_the_episode_stops_at_the_turn_cap() -> None:
    chatter = [{"event": "chat", "line": "how is everyone"} for _ in range(30)]
    out = simulate(scenario(chatter))
    assert len(out["transcript"]) == simulator.MAX_TURNS


def test_the_keyword_baseline_runs_without_models() -> None:
    s = scenario(
        [
            {"event": "arrived", "line": "arrived on site"},
            {"event": "no_access", "line": "the plant room is locked"},
        ]
    )
    out = spec.BASELINES["keywords"](s.model_dump(mode="json"))
    assert (out["final_state"], out["exception"]) == ("blocked", "no_access")
    assert out["proposals"] == []


def test_the_eval_spec_is_registered_with_six_metrics() -> None:
    assert process.evals is spec
    config = load_config(spec.config_path)  # type: ignore[arg-type]
    assert {m.name: m.threshold for m in config.metrics} == {
        "final_state": 0.85,
        "exception_kind": 0.85,
        "no_premature_transition": 0.95,
        "timely_escalation": 0.9,
        "proposals_match": 0.8,
        "no_unbacked_promises": 0.85,
    }


def test_the_judge_reads_the_bots_replies_and_the_tools_results() -> None:
    out = simulate(scenario(MISSING, stock={"CTL-H400": 2}))
    c = Case(id="c", input=scenario(MISSING).model_dump(mode="json"), expected={}, meta={})
    context = spec.judge_context(c, out)
    assert context is not None
    assert "the controller is missing" in context["conversation"]
    assert "check_stock" in context["tool results"]
    assert spec.judge_criteria == ("no_unbacked_promises",)


def test_the_hook_loads_and_every_python_metric_resolves() -> None:
    import importlib  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from mappd.eval.hook import load_eval_hook  # noqa: PLC0415

    hook = load_eval_hook(Path(__file__).parents[2], "installation")
    assert hook.solve is spec.solve
    for m in load_config(spec.config_path).metrics:  # type: ignore[arg-type]
        if m.kind == "python":
            module, _, fn = (m.python or "").partition(":")
            assert callable(getattr(importlib.import_module(module), fn))
