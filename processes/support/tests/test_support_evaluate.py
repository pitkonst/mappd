from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from mappd.data.cases import Case
from processes.support import evals as evaluate
from processes.support.data import seed


def case(citations: list[str], outcome: str = "reply") -> Case:
    return Case(
        id="c1",
        input={
            "external_id": "c1",
            "sender": "a@example.com",
            "subject": "Storage",
            "body": "full",
        },
        expected={"outcome": outcome, "citations": citations},
        meta={},
    )


def test_judge_context_carries_email_reply_outcome_and_articles() -> None:
    ctx = evaluate.judge_context(case(["kb-106"]), {"draft": {"reply": "Lower retention."}})
    assert "full" in ctx["email"]
    assert ctx["reply"] == "Lower retention."
    assert ctx["expected outcome"] == "reply"
    assert "Network Recorder NVR16" in ctx["articles"]


def test_judge_context_adds_the_articles_the_reply_cites() -> None:
    # The judge failed replies as ungrounded for steps from a second, rightly cited article.
    draft = {"reply": "Lower retention; deliveries take 2 days.", "citations": ["kb-106", "kb-202"]}
    ctx = evaluate.judge_context(case(["kb-106"]), {"draft": draft})
    assert "Network Recorder NVR16" in ctx["articles"]
    assert "kb-202" in ctx["articles"]
    assert ctx["articles"].count("kb-106") == 1


def test_clarify_context_has_no_articles_but_says_so() -> None:
    ctx = evaluate.judge_context(case([], "clarify"), {"draft": None})
    assert ctx["reply"] == ""
    assert ctx["articles"] == "(none: the reply should ask for the missing fact)"


def test_keyword_baseline_routes_and_escalates_refunds() -> None:
    out = evaluate.keyword_baseline({"subject": "Refund", "body": "I want my money back"})
    assert (out["route"], out["outcome"], out["escalate_to"]) == ("commercial", "escalate", "sales")
    out = evaluate.keyword_baseline({"subject": "Offline", "body": "sensor offline"})
    assert (out["route"], out["outcome"]) == ("technical", "reply")


def test_judge_context_loads_articles_from_seed_load_articles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def custom_articles() -> AsyncIterator[object]:
        yield SimpleNamespace(id="kb-999", title="Custom Policy", body="Always reboot twice.")

    monkeypatch.setattr(seed, "load_articles", custom_articles)
    evaluate._articles.cache_clear()
    try:
        ctx = evaluate.judge_context(case(["kb-999"]), {"draft": {"reply": "Reboot twice."}})
        assert ctx["articles"] == "kb-999: Custom Policy. Always reboot twice."
    finally:
        evaluate._articles.cache_clear()
