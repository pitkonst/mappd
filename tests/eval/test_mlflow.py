import httpx2
import pytest
from fake_mlflow import Server

from mappd.eval.mlflow import Mlflow, MlflowError, TraceRecord


def client(server: Server) -> Mlflow:
    return Mlflow("http://ml", client=httpx2.Client(transport=httpx2.MockTransport(server)))


def test_experiment_is_created_once() -> None:
    server = Server()
    ml = client(server)
    assert ml.experiment("mappd-eval-support") == "7"
    assert ml.experiment("mappd-eval-support") == "7"
    assert sum(1 for _m, p, _b in server.requests if p.endswith("/experiments/create")) == 1


def test_run_lifecycle_and_artifacts() -> None:
    server = Server()
    ml = client(server)
    run = ml.start_run("7", "dev 2026-10-13", {"mappd.set": "dev"})
    assert (run.run_id, run.metrics, run.params) == (
        "r1",
        {"routing_accuracy": 0.9},
        {"set": "dev"},
    )
    ml.log("r1", {"git_sha": "abc"}, {"routing_accuracy": 0.9})
    ml.put_artifact(run, "cases.jsonl", b'{"id": "c"}\n')
    assert ml.get_artifact(run, "cases.jsonl") == b'{"id": "c"}\n'
    assert ml.get_artifact(run, "other.jsonl") is None
    ml.finish("r1", "FINISHED")
    batch = server.batches()[0]
    assert batch["params"] == [{"key": "git_sha", "value": "abc"}]
    assert batch["metrics"][0]["key"] == "routing_accuracy"
    assert list(server.artifacts) == ["7/r1/artifacts/cases.jsonl"]


def test_latest_runs_filters_by_set() -> None:
    server = Server()
    runs = client(server).latest_runs("7", "holdout", 2)
    assert [r.run_id for r in runs] == ["r1"]
    search = next(b for _m, p, b in server.requests if p.endswith("/runs/search"))
    assert search["filter"] == "tags.`mappd.set` = 'holdout' and attributes.status = 'FINISHED'"
    assert search["max_results"] == 2


def test_logged_model_is_created_once_and_finalized() -> None:
    server = Server()
    ml = client(server)
    model_id = ml.logged_model("7", "support", "a1b2c3", {"git_sha": "abc", "prompts_sha": "def"})
    assert ml.logged_model("7", "support", "a1b2c3", {}) == model_id
    creates = [b for _m, p, b in server.requests if p.endswith("/api/2.0/mlflow/logged-models")]
    assert len(creates) == 1
    assert creates[0]["name"] == "support"
    assert creates[0]["model_type"] == "AGENT"
    assert creates[0]["tags"] == [{"key": "mappd.version", "value": "a1b2c3"}]
    params = next(
        b for _m, p, b in server.requests if p.endswith(f"/logged-models/{model_id}/params")
    )
    assert {p["key"]: p["value"] for p in params["params"]} == {
        "git_sha": "abc",
        "prompts_sha": "def",
    }
    finalize = next(
        (m, p, b)
        for m, p, b in server.requests
        if m == "PATCH" and p.endswith(f"/logged-models/{model_id}")
    )
    assert finalize[2] == {"model_id": model_id, "status": "LOGGED_MODEL_READY"}
    searches = [b for _m, p, b in server.requests if p.endswith("/logged-models/search")]
    assert len(searches) == 2
    assert searches[0]["filter"] == ("name = 'support' and tags.`mappd.version` = 'a1b2c3'")


def test_unreachable_server_is_an_mlflow_error() -> None:
    def down(_r: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    ml = Mlflow("http://ml", client=httpx2.Client(transport=httpx2.MockTransport(down)))
    with pytest.raises(MlflowError, match="refused"):
        ml.experiment("x")


def test_a_server_error_is_an_mlflow_error() -> None:
    ml = Mlflow(
        "http://ml",
        client=httpx2.Client(transport=httpx2.MockTransport(lambda _r: httpx2.Response(500))),
    )
    with pytest.raises(MlflowError, match="500"):
        ml.start_run("7", "x", {})


def test_register_and_link_traces() -> None:
    server = Server()
    ml = client(server)
    record = TraceRecord(
        trace_id="tr-abc",
        name="eval.case c1",
        start_ms=1_700_000_000_000,
        duration_ms=1250,
        state="OK",
        request_preview="Subject: Offline",
        response_preview='{"outcome": "reply"}',
        user="anna@example.com",
        session="c1",
        tags={"mappd.eval.case_id": "c1"},
        model_id="m-1",
    )
    ml.register_trace("7", "r1", record)
    ml.link_traces("r1", ["tr-abc", ""])
    trace_call = next(b for _m, p, b in server.requests if p.endswith("/api/3.0/mlflow/traces"))
    info = trace_call["trace"]["trace_info"]
    assert info["trace_id"] == "tr-abc"
    assert info["trace_location"]["mlflow_experiment"]["experiment_id"] == "7"
    assert info["execution_duration"] == "1.250s"
    assert info["trace_metadata"] == {
        "mlflow.sourceRun": "r1",
        "mlflow.trace.user": "anna@example.com",
        "mlflow.trace.session": "c1",
        "mlflow.modelId": "m-1",
    }
    assert info["tags"] == {"mlflow.traceName": "eval.case c1", "mappd.eval.case_id": "c1"}
    link_call = next(b for _m, p, b in server.requests if p.endswith("/traces/link-to-run"))
    assert link_call == {"run_id": "r1", "trace_ids": ["tr-abc"]}
