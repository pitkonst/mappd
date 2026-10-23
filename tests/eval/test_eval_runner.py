import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from dbos_async import run as arun
from fake_mlflow import Server
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from mappd.data.cache import Cache
from mappd.data.cases import Case, write_cases
from mappd.data.llm import Gateway
from mappd.eval import runner as runner_module
from mappd.eval.mlflow import Mlflow
from mappd.eval.runner import EvalReport, run_eval
from mappd.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

CONFIG = """metrics:
  - {name: routing_accuracy, kind: exact_match, field: expected.route, threshold: 0.5}
  - {name: judge_pass_rate, kind: judge, field: expected.outcome, when: {outcome: reply},
     threshold: 0.5}
"""


class Hook:
    BASELINES: Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

    def __init__(self, fail_ids: frozenset[str] = frozenset()) -> None:
        self.fail_ids = fail_ids

    async def solve(self, input: dict[str, Any]) -> dict[str, Any]:  # noqa: A002
        if input["external_id"] in self.fail_ids:
            raise RuntimeError("gateway 502")
        return {"route": "technical", "outcome": "reply", "draft": {"reply": "Do X."}}

    def judge_context(self, case: Case, output: dict[str, Any]) -> dict[str, str] | None:
        del case
        draft = output.get("draft") or {}
        return {"email": "e", "reply": str(draft.get("reply", "")), "articles": "a"}


def setup(tmp_path: Path, n: int = 4, config: str = CONFIG) -> Settings:
    evals = tmp_path / "processes" / "proc" / "evals"
    evals.mkdir(parents=True)
    (evals / "config.yaml").write_text(config)
    (evals / "rubric.md").write_text("Rubric.")
    cases = [
        Case(
            id=f"c{i}",
            input={"external_id": f"c{i}"},
            expected={"route": "technical" if i % 2 else "commercial", "outcome": "reply"},
            meta={},
        )
        for i in range(n)
    ]
    write_cases(evals / "dev.jsonl", cases)
    write_cases(evals / "holdout.jsonl", cases)
    return Settings(processes_dir=tmp_path / "processes", gateway_url="http://gw/v1")


def judge_gateway(tmp_path: Path) -> Gateway:
    verdict = json.dumps({"answers": True, "grounded": True, "language": True, "safe": True})

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, json={"model": "j", "choices": [{"message": {"content": verdict}}]}
        )

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    settings = Settings(gateway_url="http://gw/v1", gateway_config=tmp_path / "none.yaml")
    return Gateway(settings, Cache(tmp_path / "cache"), client=client)


def mlflow(server: Server) -> Mlflow:
    return Mlflow("http://ml", client=httpx2.Client(transport=httpx2.MockTransport(server)))


def evaluate(
    tmp_path: Path,
    server: Server,
    eval_set: str = "dev",
    hook: Hook | None = None,
    *,
    n: int = 4,
    config: str = CONFIG,
    ml: Mlflow | None = None,
    **kw: Any,
) -> EvalReport:
    settings = setup(tmp_path, n, config)
    hook = hook or Hook()
    return arun(
        run_eval(
            settings,
            "proc",
            eval_set=eval_set,  # type: ignore[arg-type]
            solver=hook.solve,
            hook=hook,
            gateway=judge_gateway(tmp_path),
            mlflow=ml or mlflow(server),
            **kw,
        )
    )


def test_dev_run_scores_logs_and_lists_failing_cases(tmp_path: Path) -> None:
    server = Server()
    report = evaluate(tmp_path, server)
    values = {m.name: m.value for m in report.metrics}
    assert values == {"routing_accuracy": 0.5, "judge_pass_rate": 1.0}
    assert report.failing == ["c0", "c2"]
    assert report.logged
    assert not report.failed
    assert any(k.endswith("cases.jsonl") for k in server.artifacts)
    logged = {m["key"]: m["value"] for m in server.batches()[0]["metrics"]}
    assert logged["routing_accuracy"] == 0.5
    assert logged["cases"] == 4


def test_dev_run_links_case_traces_to_the_logged_model(tmp_path: Path) -> None:
    provider = trace.get_tracer_provider()
    if isinstance(provider, trace.ProxyTracerProvider):
        trace.set_tracer_provider(TracerProvider())
    server = Server()
    evaluate(tmp_path, server)
    creates = [b for _m, p, b in server.requests if p.endswith("/api/2.0/mlflow/logged-models")]
    assert len(creates) == 1
    assert creates[0]["name"] == "proc"
    traces = [b for _m, p, b in server.requests if p.endswith("/api/3.0/mlflow/traces")]
    assert traces
    model_ids = [t["trace"]["trace_info"]["trace_metadata"].get("mlflow.modelId") for t in traces]
    assert model_ids
    assert set(model_ids) == {"m-1"}
    params = server.batches()[0]["params"]
    assert {"key": "model_id", "value": "m-1"} in params


def test_holdout_logs_aggregates_only(tmp_path: Path) -> None:
    server = Server()
    report = evaluate(tmp_path, server, "holdout")
    assert report.failing == []
    assert server.artifacts == {}
    create = next(b for _m, p, b in server.requests if p.endswith("/runs/create"))
    assert {"key": "mappd.set", "value": "holdout"} in create["tags"]


def test_holdout_refuses_a_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="--limit"):
        evaluate(tmp_path, Server(), "holdout", limit=2)


def test_errored_cases_are_counted_and_too_many_fail_the_run(tmp_path: Path) -> None:
    report = evaluate(tmp_path / "a", Server(), hook=Hook(frozenset({"c0"})), n=20)
    assert (report.errored, report.failed) == (1, False)
    report = evaluate(tmp_path / "b", Server(), hook=Hook(frozenset({"c0", "c2", "c4"})), n=20)
    assert report.errored == 3
    assert report.failed
    assert all(m.passed is not True for m in report.metrics)


def test_below_threshold_fails(tmp_path: Path) -> None:
    strict = CONFIG.replace("threshold: 0.5}", "threshold: 0.9}", 1)
    report = evaluate(tmp_path, Server(), config=strict)
    routing = next(m for m in report.metrics if m.name == "routing_accuracy")
    assert routing.passed is False
    assert report.failed


def test_mlflow_down_still_reports_and_fails(tmp_path: Path) -> None:
    def down(_r: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    ml = Mlflow("http://ml", client=httpx2.Client(transport=httpx2.MockTransport(down)))
    report = evaluate(tmp_path, Server(), ml=ml)
    assert (report.logged, report.failed) == (False, True)
    assert {m.name for m in report.metrics} == {"routing_accuracy", "judge_pass_rate"}


def test_limit_takes_the_first_cases(tmp_path: Path) -> None:
    report = evaluate(tmp_path, Server(), limit=2)
    assert report.cases == 2


def test_progress_reports_each_case(tmp_path: Path) -> None:
    calls: list[tuple[int, int]] = []
    evaluate(tmp_path, Server(), progress=lambda done, total: calls.append((done, total)))
    assert calls[0] == (0, 4)
    assert calls[-1] == (4, 4)
    assert {done for done, _ in calls} == set(range(5))


def test_a_reply_case_without_a_draft_fails_the_judge(tmp_path: Path) -> None:
    class Escalates(Hook):
        async def solve(self, input: dict[str, Any]) -> dict[str, Any]:  # noqa: A002
            del input
            return {"route": "technical", "outcome": "escalate", "draft": None}

    report = evaluate(tmp_path, Server(), hook=Escalates())
    judge = next(m for m in report.metrics if m.name == "judge_pass_rate")
    assert judge.value == 0.0


RATE = (
    CONFIG
    + """  - {name: false_escalation_rate, kind: rate, field: expected.outcome, output: outcome,
     equals: escalate, threshold: 0.15, lower_is_better: true}
"""
)


def test_a_correct_case_on_a_lower_is_better_metric_is_not_failing(tmp_path: Path) -> None:
    class AllTechnical(Hook):
        async def solve(self, input: dict[str, Any]) -> dict[str, Any]:  # noqa: A002
            del input
            return {"route": "technical", "outcome": "reply", "draft": {"reply": "Do X."}}

    report = evaluate(tmp_path, Server(), hook=AllTechnical(), config=RATE)
    assert report.failing == ["c0", "c2"], "only the routing misses, not the rate metric"


def failing_judge(tmp_path: Path, status: int | None, content: str = "") -> Gateway:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if json.loads(request.content).get("max_tokens") == 1:
            return httpx2.Response(
                200, json={"model": "j", "choices": [{"message": {"content": "x"}}]}
            )
        if status is not None:
            return httpx2.Response(status)
        return httpx2.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    settings = Settings(gateway_url="http://gw/v1", gateway_config=tmp_path / "none.yaml")
    return Gateway(settings, Cache(tmp_path / "cache"), client=client)


def run_with_judge(tmp_path: Path, server: Server, gateway: Gateway) -> EvalReport:
    settings = setup(tmp_path)
    hook = Hook()
    return arun(
        run_eval(
            settings,
            "proc",
            eval_set="dev",
            solver=hook.solve,
            hook=hook,
            gateway=gateway,
            mlflow=mlflow(server),
        )
    )


def test_a_judge_error_is_judge_errored_not_a_crash(tmp_path: Path) -> None:
    server = Server()
    report = run_with_judge(tmp_path, server, failing_judge(tmp_path, 502))
    assert report.judge_errored == 4
    assert report.cases == 4
    update = next(b for _m, p, b in server.requests if p.endswith("/runs/update"))
    assert update["status"] == "FAILED"


def test_an_unreadable_judge_fails_the_run(tmp_path: Path) -> None:
    report = run_with_judge(tmp_path, Server(), failing_judge(tmp_path, None, "I think so."))
    assert report.judge_errored == 4
    assert report.failed


def test_a_run_below_threshold_still_finishes_for_compare(tmp_path: Path) -> None:
    server = Server()
    strict = CONFIG.replace("threshold: 0.5}", "threshold: 0.9}", 1)
    report = evaluate(tmp_path, server, config=strict)
    assert report.failed
    update = next(b for _m, p, b in server.requests if p.endswith("/runs/update"))
    assert update["status"] == "FINISHED"


def test_judge_works_for_outputs_without_draft_reply_and_custom_criteria(tmp_path: Path) -> None:
    class RfqHook(Hook):
        judge_criteria = ("complete",)

        async def solve(self, input: dict[str, Any]) -> dict[str, Any]:  # noqa: A002
            del input
            return {"route": "technical", "outcome": "reply", "proposal": "10x SEN-T100"}

        def judge_context(self, case: Case, output: dict[str, Any]) -> dict[str, str]:
            del case
            return {"rfq": "Need 10 sensors", "proposal": str(output["proposal"])}

    verdict = json.dumps({"complete": True, "note": "matches"})

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, json={"model": "j", "choices": [{"message": {"content": verdict}}]}
        )

    settings = setup(tmp_path)
    hook = RfqHook()
    gw = Gateway(
        Settings(gateway_url="http://gw/v1", gateway_config=tmp_path / "none.yaml"),
        Cache(tmp_path / "cache"),
        client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    report = arun(
        run_eval(
            settings,
            "proc",
            eval_set="dev",
            solver=hook.solve,
            hook=hook,
            gateway=gw,
            mlflow=mlflow(Server()),
        )
    )
    judge_metric = next(m for m in report.metrics if m.name == "judge_pass_rate")
    assert judge_metric.value == 1.0


def test_the_feedback_set_reads_feedback_jsonl_whatever_the_hook_says(tmp_path: Path) -> None:
    hook = SimpleNamespace(dev_path=tmp_path / "dev.jsonl", holdout_path=tmp_path / "h.jsonl")
    settings = Settings(processes_dir=tmp_path)
    _, _, cases = runner_module._eval_paths(settings, "demo", "feedback", hook)
    assert cases == tmp_path / "demo" / "evals" / "feedback.jsonl"
    _, _, dev = runner_module._eval_paths(settings, "demo", "dev", hook)
    assert dev == tmp_path / "dev.jsonl"
