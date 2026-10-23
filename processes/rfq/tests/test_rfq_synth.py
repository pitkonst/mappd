from decimal import Decimal
from random import Random

from mappd.data.cases import CasePlan, Slot
from processes.rfq import process
from processes.rfq.data import synth


def test_plans_are_deterministic_by_seed() -> None:
    a = synth.plan_case(Slot(index=3, kind="quote"), Random(7))
    b = synth.plan_case(Slot(index=3, kind="quote"), Random(7))
    assert a == b


def test_facts_never_contain_labels() -> None:
    for i in range(200):
        kind = ("quote", "clarify", "escalate")[i % 3]
        plan = synth.plan_case(Slot(index=i, kind=kind), Random(i))
        text = " ".join(plan.facts).lower()
        for item in plan.labels["lines"]:
            if item["sku"]:  # a vague clarify line has none
                assert item["sku"].lower() not in text
        assert plan.labels["total"] not in text


def test_quote_totals_respect_the_threshold() -> None:
    for i in range(100):
        plan = synth.plan_case(Slot(index=i, kind="quote"), Random(i))
        assert float(plan.labels["total"]) <= 10000


def test_lines_key_is_sorted_and_stable() -> None:
    assert (
        synth.lines_key([{"sku": "SEN-T100", "quantity": 12}, {"sku": "CTL-H400", "quantity": 1}])
        == "CTL-H400x1;SEN-T100x12"
    )


def trap(name: str, i: int = 1) -> CasePlan:
    return synth.plan_case(Slot(index=i, kind=synth.TRAPS[name], trap=name), Random(i))


def test_just_under_and_over_the_threshold() -> None:
    for i in range(20):
        under = Decimal(trap("just_under_threshold", i).labels["total"])
        over = trap("just_over_threshold", i)
        assert Decimal("9000") <= under <= Decimal("10000")
        assert Decimal("10000") < Decimal(over.labels["total"]) <= Decimal("11000")
        assert (over.labels["outcome"], over.labels["escalate_to"]) == ("escalate", "sales")


def test_quantity_in_words_states_no_digit_for_that_line() -> None:
    plan = trap("quantity_in_words")
    assert any(any(w in f for w in ("dozen", "pair", "hundred")) for f in plan.facts)


def test_german_trap_is_german() -> None:
    assert trap("german").labels["language"] == "de"


def test_late_without_alternative_labels_no_alternative() -> None:
    for i in range(10):
        plan = trap("late_no_alternative", i)
        assert plan.labels["needed_by_days"] is not None
        assert plan.labels["alternatives"] == {}


def test_clarify_and_escalate_plans() -> None:
    for i in range(30):
        c = synth.plan_case(Slot(index=i, kind="clarify"), Random(i))
        assert c.labels["outcome"] == "clarify"
        assert c.omit or any("some " in f for f in c.facts)
        e = synth.plan_case(Slot(index=i, kind="escalate"), Random(i))
        assert e.labels["outcome"] == "escalate"
        assert e.labels["escalate_to"] in ("presales", "sales")


def test_spec_is_registered() -> None:
    assert process.synth is synth.spec
    assert synth.spec.strict_fields == ("outcome", "language", "lines_key")


def test_custom_escalations_keep_their_lines() -> None:
    for i in range(40):
        plan = synth.plan_case(Slot(index=i, kind="escalate"), Random(i))
        assert plan.labels["lines"], "the agent still extracts the items of a custom request"
        assert plan.labels["lines_key"] == synth.lines_key(plan.labels["lines"])


def test_a_vague_item_has_no_quantity() -> None:
    for i in range(60):
        plan = synth.plan_case(Slot(index=i, kind="clarify"), Random(i))
        if any("model not decided" in f for f in plan.facts):
            assert "?x?" in plan.labels["lines_key"]


def test_the_checker_sees_prices_to_judge_the_threshold() -> None:
    assert "48.00 EUR" in synth.CHECK_INSTRUCTIONS
