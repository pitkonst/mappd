from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from processes.support.schemas import (
    Corrections,
    Decision,
    DecisionRecord,
    Escalation,
    Resolution,
)

NOW = datetime(2026, 10, 16, tzinfo=UTC)


def test_stored_record_keeps_empty_corrections_as_an_object() -> None:
    stored = DecisionRecord(kind="send", operator="olga", decided_at=NOW).stored()
    assert stored["corrected"] == {}
    assert "agent_could_handle" not in stored
    assert stored["decided_at"] == "2026-10-16T00:00:00Z"


def test_stored_record_drops_unset_corrections() -> None:
    record = DecisionRecord(
        kind="resolve",
        operator="olga",
        corrected=Corrections(outcome="reply"),
        agent_could_handle=True,
        decided_at=NOW,
    )
    assert record.stored()["corrected"] == {"outcome": "reply"}
    assert DecisionRecord.model_validate(record.stored()) == record


@pytest.mark.parametrize("model", [Decision, Escalation, Resolution])
@pytest.mark.parametrize("operator", ["", "   "])
def test_every_decision_needs_an_author(model: type[BaseModel], operator: str) -> None:
    body = {"version": 0, "operator": operator, "reply": "Done.", "escalate_to": "billing"}
    with pytest.raises(ValidationError, match="operator"):
        model.model_validate(body)


def test_the_author_is_stored_trimmed() -> None:
    assert Decision(version=0, operator=" olga ", reply="Done.").operator == "olga"
