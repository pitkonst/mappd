import pytest
from pydantic import BaseModel, ValidationError

from processes.rfq.schemas import Decision, Escalation, Resolution


@pytest.mark.parametrize("model", [Decision, Escalation, Resolution])
@pytest.mark.parametrize("operator", ["", "   "])
def test_every_decision_needs_an_author(model: type[BaseModel], operator: str) -> None:
    body = {"version": 0, "operator": operator, "reply": "Quote.", "escalate_to": "sales"}
    with pytest.raises(ValidationError, match="operator"):
        model.model_validate(body)
