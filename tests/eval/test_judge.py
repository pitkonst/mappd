import json
from typing import TYPE_CHECKING, cast

import httpx2

from mappd.data.cache import Cache
from mappd.data.llm import Gateway
from mappd.eval.judge import JUDGE_MODEL, judge
from mappd.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

CONTEXT = {"email": "Where is my invoice?", "reply": "In the portal.", "articles": "kb-204: ..."}
ALL_TRUE = {"answers": True, "grounded": True, "language": True, "safe": True, "note": "ok"}


def gateway(tmp_path: Path, answers: list[str]) -> tuple[Gateway, list[dict[str, object]]]:
    seen: list[dict[str, object]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body.get("max_tokens") == 1:
            probe = {"model": "glm", "choices": [{"message": {"content": "x"}}]}
            return httpx2.Response(200, json=probe)
        seen.append(body)
        content = answers[len(seen) - 1]
        return httpx2.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    settings = Settings(gateway_url="http://gw/v1", gateway_config=tmp_path / "none.yaml")
    return Gateway(settings, Cache(tmp_path), client=client), seen


def test_a_fenced_verdict_is_read(tmp_path: Path) -> None:
    gw, seen = gateway(tmp_path, ["```json\n" + json.dumps(ALL_TRUE) + "\n```"])
    verdict = judge(gw, "Rubric.", CONTEXT)
    assert verdict is not None
    assert verdict.passed
    assert seen[0]["model"] == JUDGE_MODEL
    assert "Rubric." in json.dumps(seen[0]["messages"])
    assert "In the portal." in json.dumps(seen[0]["messages"])


def test_one_false_criterion_fails(tmp_path: Path) -> None:
    gw, _ = gateway(tmp_path, [json.dumps({**ALL_TRUE, "grounded": False})])
    verdict = judge(gw, "Rubric.", CONTEXT)
    assert verdict is not None
    assert not verdict.passed


def test_unparsable_verdict_retries_once_then_errors(tmp_path: Path) -> None:
    gw, seen = gateway(tmp_path, ["I think it is fine.", '{"answers": true}'])
    assert judge(gw, "Rubric.", CONTEXT) is None
    assert len(seen) == 2
    assert seen[0]["seed"] != seen[1]["seed"]


def test_retry_can_recover(tmp_path: Path) -> None:
    gw, _ = gateway(tmp_path, ["nope", json.dumps(ALL_TRUE)])
    verdict = judge(gw, "Rubric.", CONTEXT)
    assert verdict is not None
    assert verdict.passed


def test_custom_criteria_are_requested_and_checked(tmp_path: Path) -> None:
    gw, seen = gateway(
        tmp_path,
        [
            json.dumps({"complete": True, "accurate": True, "note": "good"}),
            json.dumps({"complete": True, "accurate": False, "note": "wrong price"}),
        ],
    )
    v1 = judge(
        gw,
        "RFQ rubric.",
        {"rfq": "10 sensors", "quote": "10x SEN"},
        criteria=("complete", "accurate"),
    )
    assert v1 is not None
    assert v1.passed
    system = cast("list[dict[str, str]]", seen[0]["messages"])[0]["content"]
    assert '"complete": bool, "accurate": bool' in system

    v2 = judge(
        gw,
        "RFQ rubric.",
        {"rfq": "10 sensors", "quote": "5x SEN"},
        criteria=("complete", "accurate"),
    )
    assert v2 is not None
    assert not v2.passed
