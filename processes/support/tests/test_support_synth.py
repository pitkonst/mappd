import random

import pytest

from mappd.data.cases import Message, Slot
from mappd.data.plan import make_slots
from processes.support.data import synth
from processes.support.schemas import TicketIn

WORLD = synth.WORLD


def plans(n: int = 200):  # type: ignore[no-untyped-def]
    rng = random.Random(42)
    slots = make_slots(n, synth.MIX, synth.TRAPS, synth.TRAP_RATE, rng)
    return [synth.plan_case(s, rng) for s in slots]


def test_labels_follow_the_outcome() -> None:
    for plan in plans():
        labels = plan.labels
        assert labels["outcome"] == plan.slot.kind
        assert labels["language"] == plan.customer.language
        if labels["outcome"] == "reply":
            assert labels["citations"]
            assert labels["escalate_to"] is None
            assert all(a in {x.id for x in WORLD.knowledge_base} for a in labels["citations"])
        else:
            assert labels["citations"] == []
        if labels["outcome"] == "escalate":
            WORLD.role(labels["escalate_to"])
        if labels["topic"] == "refund":
            assert labels["outcome"] == "escalate", "refunds always escalate (hard rule)"


def test_reply_cases_never_cite_the_refund_article() -> None:
    cited = {c for p in plans() for c in p.labels["citations"]}
    assert "kb-201" not in cited


def test_every_trap_is_planned_and_german_commercial_is_german() -> None:
    all_plans = plans()
    assert {p.slot.trap for p in all_plans if p.slot.trap} == set(synth.TRAPS)
    for plan in all_plans:
        if plan.slot.trap == "german_commercial":
            assert plan.customer.language == "de"
            assert plan.labels["route"] == "commercial"


def test_facts_mention_no_label_words() -> None:
    for plan in plans():
        text = " ".join(plan.facts).lower()
        assert "escalat" not in text
        assert "outcome" not in text


def test_to_input_is_a_ticket() -> None:
    plan = plans(1)[0]
    ticket = TicketIn.model_validate(synth.to_input(plan, Message(subject="Hi", body="Body")))
    assert ticket.external_id == plan.id
    assert ticket.sender == plan.customer.email


@pytest.mark.parametrize("kind", ["reply", "clarify", "escalate"])
def test_each_kind_plans_without_traps(kind: str) -> None:
    plan = synth.plan_case(Slot(index=0, kind=kind), random.Random(1))
    assert plan.labels["outcome"] == kind


def test_clarify_cases_need_what_they_leave_out() -> None:
    by_id = {a.id: a for a in WORLD.knowledge_base}
    clarify = [p for p in plans() if p.slot.kind == "clarify"]
    assert clarify
    for plan in clarify:
        article = next(a for a in WORLD.knowledge_base if a.question in plan.facts[0])
        assert by_id[article.id].needs, f"{article.id} can be answered without more facts"
        assert plan.omit


def test_no_fact_states_the_answer() -> None:
    for plan in plans():
        text = " ".join(plan.facts).lower()
        assert "does not say" not in text
        assert "documentation does not" not in text


def test_checker_is_told_which_missing_facts_make_a_clarify() -> None:
    for fact in synth.OMISSIONS.values():
        assert fact in synth.CHECK_INSTRUCTIONS


def test_checker_is_told_every_kind_of_escalation() -> None:
    assert set(synth.ESCALATION_REASONS) == set(synth.ESCALATIONS)
    for reason in synth.ESCALATION_REASONS.values():
        assert reason in synth.CHECK_INSTRUCTIONS


def test_reply_to_a_device_article_names_one_of_its_devices() -> None:
    rng = random.Random(3)
    seen = 0
    for i in range(300):
        plan = synth.plan_case(Slot(index=i, kind="reply", trap=None), rng)
        article = next(a for a in WORLD.knowledge_base if a.id == plan.labels["citations"][0])
        if "device" in article.needs:
            seen += 1
            names = {p.name for p in WORLD.products_in(article)}
            assert any(name in " ".join(plan.facts) for name in names)
    assert seen > 0


def test_refund_without_word_tells_the_writer_to_avoid_the_word() -> None:
    slot = Slot(index=0, kind="escalate", trap="refund_without_word")
    plan = synth.plan_case(slot, random.Random(5))
    assert any("refund" in o for o in plan.omit)
    assert any("Rückerstattung" in o for o in plan.omit)


def test_checker_knows_the_hard_escalations() -> None:
    # Traps the round trip must keep: a refund in other words, an integration question.
    assert "money back" in synth.CHECK_INSTRUCTIONS
    assert "integrations" in synth.CHECK_INSTRUCTIONS


def test_checker_knows_which_questions_depend_on_the_model() -> None:
    for article in WORLD.knowledge_base:
        if "device" in article.needs:
            assert article.question in synth.CHECK_INSTRUCTIONS


def test_checker_knows_integration_questions_escalate_rather_than_clarify() -> None:
    assert "works with another system" in synth.CHECK_INSTRUCTIONS
