from datetime import UTC, datetime

from processes.support.domain import (
    approval_workflow_id,
    auto_sends,
    decide,
    draft_prompt,
    escalation_record,
    resolution_record,
    send_record,
    ticket_text,
    ticket_workflow_id,
)
from processes.support.schemas import (
    Classification,
    Escalation,
    Resolution,
    SupportOutput,
)


def classification(
    topic: str = "how_to",
    *,
    route: str = "technical",
    outcome: str = "reply",
    escalate_to: str | None = None,
    safety_risk: bool = False,
) -> Classification:
    return Classification.model_validate(
        {
            "topic": topic,
            "route": route,
            "urgency": "normal",
            "language": "en",
            "safety_risk": safety_risk,
            "outcome": outcome,
            "escalate_to": escalate_to,
        }
    )


def test_refunds_always_escalate_to_sales_whatever_the_model_says() -> None:
    refund = classification("refund", route="commercial", outcome="reply")
    assert decide(refund) == ("escalate", "sales")
    misrouted = classification("refund", outcome="escalate", escalate_to="service")
    assert decide(misrouted) == ("escalate", "sales")


def test_safety_risks_always_escalate_to_service() -> None:
    smoke = classification("malfunction", outcome="reply", safety_risk=True)
    assert decide(smoke) == ("escalate", "service")


def test_the_models_outcome_stands_otherwise() -> None:
    assert decide(classification(outcome="clarify")) == ("clarify", None)
    assert decide(classification(outcome="reply", escalate_to="sales")) == ("reply", None)
    quote = classification(outcome="escalate", escalate_to="presales")
    assert decide(quote) == ("escalate", "presales")


def test_an_escalation_without_a_role_goes_by_route() -> None:
    assert decide(classification(route="commercial", outcome="escalate")) == ("escalate", "sales")
    assert decide(classification(route="technical", outcome="escalate")) == ("escalate", "service")


def test_ticket_text_joins_subject_and_body() -> None:
    assert ticket_text("Sensor offline", "Room 2") == "Subject: Sensor offline\n\nRoom 2"


def test_draft_prompt_starts_with_the_ticket() -> None:
    assert draft_prompt("Subject: S\n\nB", "reply", "en").startswith("Subject: S\n\nB")


def test_draft_prompt_names_the_detected_language() -> None:
    # A short email gives the drafter little to go on: it wrote English tickets in French.
    assert "Write the reply in English (en)." in draft_prompt("Subject: S\n\nB", "reply", "en")
    assert "Write the reply in German (de)." in draft_prompt("Subject: S\n\nB", "clarify", "de")


def test_draft_prompt_names_an_unlisted_language_by_its_code() -> None:
    assert "language code fi" in draft_prompt("Subject: S\n\nB", "reply", "fi")


def test_draft_prompt_on_clarify_asks_for_the_missing_fact() -> None:
    prompt = draft_prompt("Subject: S\n\nB", "clarify", "en")
    assert prompt.startswith("Subject: S\n\nB")
    assert "missing fact" in prompt
    assert "missing fact" not in draft_prompt("Subject: S\n\nB", "reply", "en")


def test_workflow_ids_come_from_the_external_id_never_a_counter() -> None:
    assert ticket_workflow_id("m-1") == "support.ticket:m-1"
    assert approval_workflow_id("m-1", 2) == "support.approve:m-1:v2"


NOW = datetime(2026, 10, 16, tzinfo=UTC)


def test_send_record_marks_edits_and_real_corrections_only() -> None:
    same = send_record(
        operator="olga",
        shadow=False,
        reply="A",
        draft="A",
        topic="how_to",
        agent_topic="how_to",
        now=NOW,
    )
    assert (same.edited_reply, same.corrected.topic) == (False, None)
    edited = send_record(
        operator="olga",
        shadow=True,
        reply="B",
        draft="A",
        topic="billing",
        agent_topic="how_to",
        now=NOW,
    )
    assert (edited.edited_reply, edited.corrected.topic, edited.shadow) == (True, "billing", True)


def test_only_replies_and_clarifications_auto_send() -> None:
    assert auto_sends("reply", "auto")
    assert auto_sends("clarify", "auto")
    assert not auto_sends("escalate", "auto")
    assert not auto_sends("reply", "review")


def test_escalation_records_the_outcome_and_team() -> None:
    r = escalation_record(
        Escalation(version=1, operator="olga", escalate_to="billing"), agent_topic="how_to", now=NOW
    )
    assert (r.kind, r.corrected.outcome, r.corrected.escalate_to, r.corrected.topic) == (
        "escalate",
        "escalate",
        "billing",
        None,
    )


def test_resolution_marks_a_false_escalation() -> None:
    agent = SupportOutput(
        topic="how_to",
        route="technical",
        urgency="low",
        language="en",
        outcome="escalate",
        escalate_to="service",
        draft=None,
    )
    r = resolution_record(
        Resolution(version=1, operator="olga", agent_could_handle=True, outcome="reply"),
        agent=agent,
        now=NOW,
    )
    assert (r.kind, r.agent_could_handle, r.corrected.outcome) == ("resolve", True, "reply")
    right_team = resolution_record(Resolution(version=1, operator="olga"), agent=agent, now=NOW)
    assert right_team.corrected.model_dump(exclude_none=True) == {}
