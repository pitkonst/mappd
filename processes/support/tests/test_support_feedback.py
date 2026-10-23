from typing import Any

from processes.support.feedback import TicketRow, to_case

OUTPUT: dict[str, Any] = {
    "topic": "how_to",
    "route": "technical",
    "urgency": "low",
    "language": "en",
    "outcome": "reply",
    "escalate_to": None,
    "draft": {"reply": "Do X.", "citations": ["kb-101"]},
}
NOW = "2026-10-16T00:00:00Z"


def ticket(decision: dict[str, Any], output: dict[str, Any] = OUTPUT) -> TicketRow:
    return TicketRow.model_validate(
        {
            "external_id": "m-1",
            "sender": "anna@example.com",
            "subject": "S",
            "body": "B",
            "output": output,
            "decision": decision,
        }
    )


def test_an_unedited_send_keeps_the_agent_labels_and_citations() -> None:
    c = to_case(ticket({"kind": "send", "operator": "olga", "corrected": {}, "decided_at": NOW}))
    assert c is not None
    assert c.id == "fb-m-1"
    assert c.expected["outcome"] == "reply"
    assert c.expected["citations"] == ["kb-101"]
    assert c.meta["edited"] is False


def test_an_edited_send_drops_citations_and_applies_the_topic() -> None:
    c = to_case(
        ticket(
            {
                "kind": "send",
                "operator": "olga",
                "edited_reply": True,
                "corrected": {"topic": "billing"},
                "decided_at": NOW,
            }
        )
    )
    assert c is not None
    assert (c.expected["topic"], c.expected["citations"], c.meta["edited"]) == ("billing", [], True)
    assert c.meta["corrected_fields"] == ["topic"]


def test_an_operator_escalation_expects_escalate_to_the_team() -> None:
    c = to_case(
        ticket(
            {
                "kind": "escalate",
                "operator": "olga",
                "corrected": {"outcome": "escalate", "escalate_to": "billing"},
                "decided_at": NOW,
            }
        )
    )
    assert c is not None
    assert (c.expected["outcome"], c.expected["escalate_to"]) == ("escalate", "billing")


def test_a_false_escalation_expects_the_corrected_outcome() -> None:
    esc = {**OUTPUT, "outcome": "escalate", "escalate_to": "service", "draft": None}
    c = to_case(
        ticket(
            {
                "kind": "resolve",
                "operator": "olga",
                "agent_could_handle": True,
                "corrected": {"outcome": "reply"},
                "decided_at": NOW,
            },
            esc,
        )
    )
    assert c is not None
    assert (c.expected["outcome"], c.expected["escalate_to"]) == ("reply", None)


def test_auto_decisions_are_skipped() -> None:
    assert (
        to_case(ticket({"kind": "send", "operator": "auto", "corrected": {}, "decided_at": NOW}))
        is None
    )


def test_a_resolved_operator_escalation_keeps_the_team() -> None:
    prior = {
        "kind": "escalate",
        "operator": "olga",
        "decided_at": NOW,
        "corrected": {"outcome": "escalate", "escalate_to": "billing", "topic": "billing"},
    }
    c = to_case(
        ticket(
            {
                "kind": "resolve",
                "operator": "ivan",
                "agent_could_handle": False,
                "corrected": {},
                "decided_at": NOW,
                "prior": prior,
            }
        )
    )
    assert c is not None
    assert (c.expected["outcome"], c.expected["escalate_to"], c.expected["topic"]) == (
        "escalate",
        "billing",
        "billing",
    )


def test_a_topic_only_correction_keeps_citations() -> None:
    c = to_case(
        ticket(
            {
                "kind": "send",
                "operator": "olga",
                "corrected": {"topic": "billing"},
                "decided_at": NOW,
            }
        )
    )
    assert c is not None
    assert c.expected["citations"] == ["kb-101"]
