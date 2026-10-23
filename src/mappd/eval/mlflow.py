"""The few MLflow REST calls the evals need: experiments, runs, metrics, artifacts."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import httpx2

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

TIMEOUT = 30.0
NOT_FOUND = 404
API = "/api/2.0/mlflow"
API_V3 = "/api/3.0/mlflow"
ARTIFACTS = "/api/2.0/mlflow-artifacts/artifacts"
PREVIEW_LIMIT = 1000
MAX_TRACE_LINKS = 100


@dataclass(frozen=True)
class TraceRecord:
    """One case trace to file under an eval experiment and link to its run."""

    trace_id: str
    name: str
    start_ms: int
    duration_ms: int
    state: Literal["OK", "ERROR"]
    request_preview: str
    response_preview: str
    user: str | None = None
    session: str | None = None
    tags: Mapping[str, str] | None = None
    model_id: str | None = None


class MlflowError(RuntimeError):
    """MLflow is unreachable or refused a call."""


@dataclass(frozen=True)
class RunRecord:
    """One MLflow run as the evals read it."""

    run_id: str
    start_time: int
    params: dict[str, str]
    tags: dict[str, str]
    metrics: dict[str, float]
    artifact_uri: str


def _pairs(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {i["key"]: i["value"] for i in items or []}


def _record(run: dict[str, Any]) -> RunRecord:
    info, data = run["info"], run.get("data", {})
    return RunRecord(
        run_id=info["run_id"],
        start_time=int(info.get("start_time", 0)),
        params={k: str(v) for k, v in _pairs(data.get("params")).items()},
        tags={k: str(v) for k, v in _pairs(data.get("tags")).items()},
        metrics={k: float(v) for k, v in _pairs(data.get("metrics")).items()},
        artifact_uri=info.get("artifact_uri", ""),
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


class Mlflow:
    """A synchronous MLflow REST client."""

    def __init__(self, base_url: str, client: httpx2.Client | None = None) -> None:
        """Talk to the MLflow server at `base_url`."""
        self._base = base_url.rstrip("/")
        self._client = client or httpx2.Client(timeout=TIMEOUT)

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        params: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx2.Response:
        """The response; a 404 is returned for the caller to judge, other errors raise."""
        try:
            response = self._client.request(
                method, self._base + path, json=body, params=params, content=content
            )
        except httpx2.HTTPError as e:
            raise MlflowError(f"MLflow at {self._base}: {e}") from e
        if response.is_error and response.status_code != NOT_FOUND:
            raise MlflowError(
                f"MLflow {method} {path}: {response.status_code} {response.text[:200]}"
            )
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._call(method, path, **kwargs)
        if response.status_code == NOT_FOUND:
            raise MlflowError(f"MLflow {method} {path}: 404 {response.text[:200]}")
        data: dict[str, Any] = response.json()
        return data

    def experiment(self, name: str) -> str:
        """The experiment's ID, created on first use."""
        found = self._call(
            "GET", f"{API}/experiments/get-by-name", params={"experiment_name": name}
        )
        if found.status_code != NOT_FOUND:
            return str(found.json()["experiment"]["experiment_id"])
        created = self._json("POST", f"{API}/experiments/create", body={"name": name})
        return str(created["experiment_id"])

    def start_run(self, experiment_id: str, name: str, tags: Mapping[str, str]) -> RunRecord:
        """A new run with `tags`."""
        body = {
            "experiment_id": experiment_id,
            "run_name": name,
            "start_time": _now_ms(),
            "tags": [{"key": k, "value": v} for k, v in tags.items()],
        }
        return _record(self._json("POST", f"{API}/runs/create", body=body)["run"])

    def log(self, run_id: str, params: Mapping[str, str], metrics: Mapping[str, float]) -> None:
        """Log params and metrics in one batch."""
        now = _now_ms()
        body = {
            "run_id": run_id,
            "params": [{"key": k, "value": v} for k, v in params.items()],
            "metrics": [
                {"key": k, "value": v, "timestamp": now, "step": 0} for k, v in metrics.items()
            ],
        }
        self._json("POST", f"{API}/runs/log-batch", body=body)

    def _artifact_path(self, run: RunRecord, path: str) -> str:
        return f"{ARTIFACTS}/{run.artifact_uri.removeprefix('mlflow-artifacts:/')}/{path}"

    def put_artifact(self, run: RunRecord, path: str, content: bytes) -> None:
        """Upload one artifact file."""
        response = self._call("PUT", self._artifact_path(run, path), content=content)
        if response.status_code == NOT_FOUND:
            raise MlflowError("MLflow does not serve artifacts (start it with --serve-artifacts)")

    def get_artifact(self, run: RunRecord, path: str) -> bytes | None:
        """Download one artifact file; None if the run has none at `path`."""
        response = self._call("GET", self._artifact_path(run, path))
        return None if response.status_code == NOT_FOUND else response.content

    def finish(self, run_id: str, status: Literal["FINISHED", "FAILED"]) -> None:
        """Close the run."""
        body = {"run_id": run_id, "status": status, "end_time": _now_ms()}
        self._json("POST", f"{API}/runs/update", body=body)

    def get_run(self, run_id: str) -> RunRecord:
        """One run by ID."""
        return _record(self._json("GET", f"{API}/runs/get", params={"run_id": run_id})["run"])

    def latest_runs(self, experiment_id: str, eval_set: str, n: int) -> list[RunRecord]:
        """The newest `n` complete runs of one eval set, newest first (crashed runs left out)."""
        body = {
            "experiment_ids": [experiment_id],
            "filter": f"tags.`mappd.set` = '{eval_set}' and attributes.status = 'FINISHED'",
            "order_by": ["attributes.start_time DESC"],
            "max_results": n,
        }
        runs = self._json("POST", f"{API}/runs/search", body=body).get("runs", [])
        return [_record(r) for r in runs]

    def logged_model(
        self, experiment_id: str, name: str, version: str, params: Mapping[str, str]
    ) -> str:
        """The `name` agent's logged model for `version` — the Versions tab's entry — once."""
        found = self._json(
            "POST",
            f"{API}/logged-models/search",
            body={
                "experiment_ids": [experiment_id],
                "filter": f"name = '{name}' and tags.`mappd.version` = '{version}'",
                "max_results": 1,
            },
        ).get("models", [])
        if found:
            return str(found[0]["info"]["model_id"])
        created = self._json(
            "POST",
            f"{API}/logged-models",
            body={
                "experiment_id": experiment_id,
                "name": name,
                "model_type": "AGENT",
                "tags": [{"key": "mappd.version", "value": version}],
            },
        )
        model_id = str(created["model"]["info"]["model_id"])
        self._json(
            "POST",
            f"{API}/logged-models/{model_id}/params",
            body={
                "model_id": model_id,
                "params": [{"key": k, "value": v} for k, v in params.items()],
            },
        )
        self._json(
            "PATCH",
            f"{API}/logged-models/{model_id}",
            body={"model_id": model_id, "status": "LOGGED_MODEL_READY"},
        )
        return model_id

    def register_trace(self, experiment_id: str, run_id: str | None, trace: TraceRecord) -> None:
        """Upsert `trace` into `experiment_id` (moving any gateway spans) and attach `run_id`."""
        ts = (
            datetime.fromtimestamp(trace.start_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[
                :-3
            ]
            + "Z"
        )
        metadata: dict[str, str] = {}
        if run_id:
            metadata["mlflow.sourceRun"] = run_id
        if trace.user:
            metadata["mlflow.trace.user"] = trace.user
        if trace.session:
            metadata["mlflow.trace.session"] = trace.session
        if trace.model_id:
            metadata["mlflow.modelId"] = trace.model_id
        tags = {"mlflow.traceName": trace.name, **(trace.tags or {})}
        info: dict[str, Any] = {
            "trace_id": trace.trace_id,
            "trace_location": {
                "type": "MLFLOW_EXPERIMENT",
                "mlflow_experiment": {"experiment_id": experiment_id},
            },
            "request_time": ts,
            "execution_duration": f"{max(trace.duration_ms, 0) / 1000:.3f}s",
            "state": trace.state,
            "trace_metadata": metadata,
            "tags": tags,
            "request_preview": trace.request_preview[:PREVIEW_LIMIT],
            "response_preview": trace.response_preview[:PREVIEW_LIMIT],
        }
        self._json("POST", f"{API_V3}/traces", body={"trace": {"trace_info": info}})

    def link_traces(self, run_id: str, trace_ids: Sequence[str]) -> None:
        """Associate `trace_ids` with `run_id` so the run's Traces tab lists them."""
        ids = [t for t in trace_ids if t]
        for i in range(0, len(ids), MAX_TRACE_LINKS):
            chunk = ids[i : i + MAX_TRACE_LINKS]
            self._json(
                "POST",
                f"{API_V3}/traces/link-to-run",
                body={"run_id": run_id, "trace_ids": chunk},
            )
