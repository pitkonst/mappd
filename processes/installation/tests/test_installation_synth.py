"""Synthesized installation scenarios: a job, hidden facts, a script, and what should happen."""

import json
from pathlib import Path
from random import Random

import pytest
from typer.testing import CliRunner

from mappd.cli import app
from mappd.data.cases import Message, Slot
from processes.installation import process, sim
from processes.installation.data import synth

REPO = Path(__file__).parents[3]


def plan(kind: str, trap: str | None = None, seed: int = 1) -> synth.CasePlan:
    return synth.plan_case(Slot(index=seed, kind=kind, trap=trap), Random(seed))


def events(p: synth.CasePlan) -> list[str]:
    return [s["event"] for s in p.labels["script"]]


def test_plans_are_deterministic_by_seed() -> None:
    assert plan("missing_parts", seed=3) == plan("missing_parts", seed=3)


def test_a_happy_job_walks_every_step_with_photos_as_evidence() -> None:
    p = plan("happy")
    assert events(p) == list(synth.FLOW)
    photos = {s["event"]: s["photo"] for s in p.labels["script"]}
    assert photos["installed"] == photos["tested"] == "yes"
    assert p.labels["final_state"] == "handed_over"
    assert (p.labels["exception"], p.labels["exception_kind"]) == (None, "none")
    assert (p.labels["proposals"], p.labels["ticket"]) == ([], False)


def test_a_missing_part_in_stock_expects_a_reservation() -> None:
    p = plan("missing_parts", trap="missing_but_in_stock")
    sku = p.labels["proposals"][0]["sku"]
    assert p.labels["facts"]["stock"][sku] > 0
    assert p.labels["proposals"] == [{"tool": "reserve_parts", "sku": sku}]
    assert events(p)[-1] == "missing_parts"
    assert (p.labels["final_state"], p.labels["exception"]) == ("blocked", "missing_parts")


def test_a_missing_part_out_of_stock_expects_a_delivery() -> None:
    p = plan("missing_parts", trap="missing_none_in_stock")
    (proposal,) = p.labels["proposals"]
    assert proposal["tool"] == "book_delivery"
    missing = p.labels["missing_sku"]
    assert p.labels["facts"]["stock"].get(missing, 0) == 0
    site = p.labels["job"]["site"]
    first = next(s["at"] for s in p.labels["facts"]["slots"] if s["site"] in (site, None))
    assert proposal == {"tool": "book_delivery", "slot": first}


def test_breakage_expects_a_ticket_and_a_replacement() -> None:
    p = plan("breakage", seed=5)
    assert p.labels["ticket"] is True
    assert p.labels["facts"]["damaged"] == [p.labels["missing_sku"]]
    assert p.labels["proposals"][0]["tool"] in {"reserve_parts", "book_delivery"}
    assert events(p)[-1] == "breakage"


def test_no_access_stops_at_the_door_and_proposes_nothing() -> None:
    p = plan("no_access")
    assert events(p) == ["arrived", "no_access"]
    assert p.labels["proposals"] == []
    assert p.labels["job"]["site"] in p.labels["facts"]["contacts"]


def test_an_incident_stops_the_job_and_proposes_nothing() -> None:
    # A real installer forced his way past a guard; the job carried on as if it were progress.
    p = plan("incident")
    assert events(p) == ["arrived", "incident"]
    assert (p.labels["final_state"], p.labels["exception"]) == ("blocked", "incident")
    assert p.labels["proposals"] == []
    assert "guard" in p.labels["script"][1]["fact"]
    assert synth.MIX["incident"] > 0


def test_arrival_with_a_problem_is_one_message() -> None:
    # "I arrived, the gate is closed" was read as a question twice: no arrival, no block.
    p = plan("no_access", trap="arrival_with_problem")
    assert events(p) == ["no_access"]
    fact = p.labels["script"][0]["fact"]
    assert p.labels["job"]["site"] in fact
    assert "locked" in fact
    assert synth.TRAPS["arrival_with_problem"] == "no_access"
    assert "incident" in synth.CHECK_INSTRUCTIONS


def test_traps_shape_the_script() -> None:
    late = plan("happy", trap="photo_late")
    assert {s["event"]: s["photo"] for s in late.labels["script"]}["installed"] == "late"
    chatty = plan("happy", trap="chatty_detour")
    assert "chat" in events(chatty)
    assert events(chatty)[-1] == "handed_over"


def test_facts_never_contain_labels() -> None:
    for i in range(40):
        kind = ("happy", "missing_parts", "breakage", "no_access")[i % 4]
        p = plan(kind, seed=i)
        text = " ".join(p.facts).lower()
        assert p.labels["exception_kind"] not in text.replace(" ", "_")
        assert "reserve_parts" not in text
        assert "book_delivery" not in text


def test_each_installer_is_one_persona() -> None:
    personas = {plan("happy", seed=i).customer.persona.id for i in range(60)}  # type: ignore[union-attr]
    assert len(personas) == len(synth.CREW)


def test_to_input_puts_each_line_on_its_step() -> None:
    p = plan("no_access")
    body = "1. On site now.\n2. The plant room is locked, nobody here has a key."
    scenario = sim.Scenario.model_validate(synth.to_input(p, Message(subject="x", body=body)))
    assert [s.line for s in scenario.script] == [
        "On site now.",
        "The plant room is locked, nobody here has a key.",
    ]
    assert scenario.job.external_id == p.id
    assert scenario.persona == p.customer.persona.style  # type: ignore[union-attr]


def test_to_input_falls_back_to_the_fact_for_a_missing_line() -> None:
    p = plan("no_access")
    scenario = sim.Scenario.model_validate(
        synth.to_input(p, Message(subject="x", body="1. On site now."))
    )
    assert scenario.script[0].line == "On site now."
    assert scenario.script[1].line == p.labels["script"][1]["fact"]


def test_spec_is_registered() -> None:
    assert process.synth is synth.spec
    assert synth.spec.strict_fields == ("exception_kind",)


def test_dry_run_plans_the_mix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAPPD_PROCESSES_DIR", str(REPO / "processes"))
    monkeypatch.setenv("MAPPD_WORLD_PATH", str(REPO / "world.yaml"))
    args = ["data", "synth", "installation", "--seed", "42", "--n", "20", "--dry-run"]
    r = CliRunner().invoke(app, [*args, "--out", str(tmp_path), "--json"])
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout)["planned"] == {
        "happy": 7,
        "missing_parts": 5,
        "breakage": 3,
        "no_access": 3,
        "incident": 2,
    }
