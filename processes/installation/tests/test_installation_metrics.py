"""The simulation metrics on hand-built episodes."""

from typing import Any

from mappd.data.cases import Case
from processes.installation.evals import metrics


def case(**expected: Any) -> Case:
    base: dict[str, Any] = {
        "final_state": "handed_over",
        "exception": None,
        "proposals": [],
        "ticket": False,
    }
    return Case(id="c", input={}, expected={**base, **expected}, meta={})


def episode(**output: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "states": [],
        "true_states": [],
        "revealed_turn": None,
        "blocked_turn": None,
        "proposals": [],
    }
    return {**base, **output}


def test_timely_escalation_scores_false_blocks_and_misses() -> None:
    happy, missing = case(), case(exception="missing_parts", final_state="blocked")
    assert metrics.timely_escalation(happy, episode()) == 1.0
    assert metrics.timely_escalation(happy, episode(blocked_turn=2)) == 0.0, "a false block"
    assert metrics.timely_escalation(missing, episode(revealed_turn=2)) == 0.0, "a miss"
    assert metrics.timely_escalation(missing, episode(revealed_turn=2, blocked_turn=2)) == 1.0
    assert metrics.timely_escalation(missing, episode(revealed_turn=2, blocked_turn=3)) == 1.0
    assert metrics.timely_escalation(missing, episode(revealed_turn=2, blocked_turn=4)) == 0.0


def test_a_state_ahead_of_the_truth_is_premature() -> None:
    truth = ["arrived", "access", "access"]
    on_time = episode(states=["arrived", "access", "access"], true_states=truth)
    behind = episode(states=["assigned", "arrived", "access"], true_states=truth)
    ahead = episode(states=["arrived", "equipment_found", "access"], true_states=truth)
    blocked = episode(states=["arrived", "blocked"], true_states=["arrived", "access"])
    assert metrics.no_premature_transition(case(), on_time) == 1.0
    assert metrics.no_premature_transition(case(), behind) == 1.0
    assert metrics.no_premature_transition(case(), ahead) == 0.0
    assert metrics.no_premature_transition(case(), blocked) == 1.0


def test_proposals_match_by_tool_and_key_argument() -> None:
    reserve = {"tool": "reserve_parts", "sku": "CTL-H400"}
    expected = case(exception="missing_parts", final_state="blocked", proposals=[reserve])
    exact = episode(
        proposals=[{"tool": "reserve_parts", "arguments": {"job": "j", "skus": ["CTL-H400"]}}]
    )
    wrong = episode(
        proposals=[{"tool": "book_delivery", "arguments": {"job": "j", "slot": "2026-10-23"}}]
    )
    extra = episode(
        proposals=[
            {"tool": "reserve_parts", "arguments": {"job": "j", "skus": ["CTL-H400", "SEN-T100"]}}
        ]
    )
    assert metrics.proposals_match(expected, exact) == 1.0
    assert metrics.proposals_match(expected, wrong) == 0.0
    assert metrics.proposals_match(expected, extra) == 2 * 0.5 * 1 / 1.5
    no_access = case(exception="no_access", final_state="blocked")
    assert metrics.proposals_match(no_access, episode()) == 1.0
    assert metrics.proposals_match(case(), episode()) is None, "a happy job is out of scope"
