from typing import Any

from processes.rfq import domain
from processes.rfq.feedback import RfqRow, to_case
from processes.rfq.schemas import Extracted, Extraction

NOW = "2026-10-16T00:00:00Z"
OUTPUT = domain.solve_output(
    Extraction(
        lines=[
            Extracted(requested="sensors", quantity=12, sku="SEN-T100", confidence="high"),
            Extracted(requested="hub", quantity=1, sku="CTL-H400", confidence="high"),
        ],
        needed_by_days=None,
        custom_request=False,
        language="en",
    )
).model_dump(mode="json")


def row(decision: dict[str, Any], output: dict[str, Any] = OUTPUT) -> RfqRow:
    return RfqRow.model_validate(
        {
            "external_id": "r-1",
            "sender": "anna@example.com",
            "subject": "S",
            "body": "B",
            "output": output,
            "decision": decision,
        }
    )


def test_an_unedited_send_keeps_the_agent_lines() -> None:
    c = to_case(row({"kind": "send", "operator": "olga", "corrected": {}, "decided_at": NOW}))
    assert c is not None
    assert c.id == "fb-r-1"
    assert c.expected["lines"] == [
        {"sku": "SEN-T100", "quantity": 12},
        {"sku": "CTL-H400", "quantity": 1},
    ]
    assert c.expected["lines_key"] == "CTL-H400x1;SEN-T100x12"
    assert c.expected["total"] == "1266.00"
    assert c.meta["edited"] is False


def test_corrected_lines_become_the_expected_lines() -> None:
    c = to_case(
        row(
            {
                "kind": "send",
                "operator": "olga",
                "decided_at": NOW,
                "corrected": {"lines": [{"sku": "SEN-T100", "quantity": 2}]},
            }
        )
    )
    assert c is not None
    assert c.expected["lines"] == [{"sku": "SEN-T100", "quantity": 2}]
    assert (c.expected["lines_key"], c.expected["total"]) == ("SEN-T100x2", "96.00")
    assert c.meta["corrected_fields"] == ["lines"]


def test_an_operator_escalation_expects_escalate_to_the_team() -> None:
    c = to_case(
        row(
            {
                "kind": "escalate",
                "operator": "olga",
                "decided_at": NOW,
                "corrected": {"outcome": "escalate", "escalate_to": "sales"},
            }
        )
    )
    assert c is not None
    assert (c.expected["outcome"], c.expected["escalate_to"]) == ("escalate", "sales")


def test_a_false_escalation_expects_the_corrected_outcome() -> None:
    esc = {**OUTPUT, "outcome": "escalate", "escalate_to": "presales", "draft": None}
    c = to_case(
        row(
            {
                "kind": "resolve",
                "operator": "olga",
                "agent_could_handle": True,
                "corrected": {"outcome": "quote"},
                "decided_at": NOW,
            },
            esc,
        )
    )
    assert c is not None
    assert (c.expected["outcome"], c.expected["escalate_to"]) == ("quote", None)


def test_auto_decisions_are_skipped() -> None:
    assert (
        to_case(row({"kind": "send", "operator": "auto", "corrected": {}, "decided_at": NOW}))
        is None
    )


def test_corrected_lines_set_the_outcome_too() -> None:
    clarify = {**OUTPUT, "outcome": "clarify", "draft": "Before we can quote..."}
    c = to_case(
        row(
            {
                "kind": "send",
                "operator": "olga",
                "decided_at": NOW,
                "corrected": {"lines": [{"sku": "SEN-T100", "quantity": 2}]},
            },
            clarify,
        )
    )
    assert c is not None
    assert (c.expected["outcome"], c.expected["escalate_to"]) == ("quote", None)
