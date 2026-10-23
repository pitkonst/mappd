from typing import Any

from dbos_async import run as arun
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from processes.support import agents
from processes.support.schemas import SupportOutput, TicketIn
from processes.support.workflow import solve

TICKET = TicketIn(external_id="t-1", sender="a@example.com", subject="Help", body="My NVR is full")


def classifier_says(**fields: Any) -> FunctionModel:
    base = {
        "topic": "malfunction",
        "route": "technical",
        "urgency": "normal",
        "language": "en",
        "safety_risk": False,
        "outcome": "reply",
        "escalate_to": None,
    }

    def answer(_m: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**base, **fields})])

    return FunctionModel(answer)


def drafter_records(prompts: list[str]) -> FunctionModel:
    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(str(messages[0]))
        args = {"reply": "Do X.", "citations": ["kb-106"]}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    return FunctionModel(answer)


def run(classifier: FunctionModel, drafter: FunctionModel) -> SupportOutput:
    with agents.classifier.override(model=classifier), agents.drafter.override(model=drafter):
        return arun(solve(TICKET))


def test_reply_gets_a_draft() -> None:
    prompts: list[str] = []
    out = run(classifier_says(), drafter_records(prompts))
    assert (out.outcome, out.escalate_to) == ("reply", None)
    assert out.draft is not None
    assert out.draft.citations == ["kb-106"]
    assert "missing" not in prompts[0].lower()


def test_clarify_drafts_a_question() -> None:
    prompts: list[str] = []
    out = run(classifier_says(outcome="clarify"), drafter_records(prompts))
    assert out.outcome == "clarify"
    assert out.draft is not None
    assert "ask" in prompts[0].lower()
    assert "missing" in prompts[0].lower()


def test_escalation_has_no_draft_and_never_calls_the_drafter() -> None:
    prompts: list[str] = []
    out = run(classifier_says(topic="refund", outcome="reply"), drafter_records(prompts))
    assert (out.outcome, out.escalate_to, out.draft) == ("escalate", "sales", None)
    assert prompts == []


def test_instructions_come_from_the_prompt_files() -> None:
    assert agents.CLASSIFIER_PROMPT.read_text() == agents.CLASSIFIER_INSTRUCTIONS
    assert agents.DRAFTER_PROMPT.read_text() == agents.DRAFTER_INSTRUCTIONS
