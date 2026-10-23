"""A fake MLflow REST server for httpx2.MockTransport: experiments, runs, artifacts."""

import json
from typing import Any

import httpx2

RUN = {
    "info": {"run_id": "r1", "start_time": 5, "artifact_uri": "mlflow-artifacts:/7/r1/artifacts"},
    "data": {
        "params": [{"key": "set", "value": "dev"}],
        "tags": [{"key": "mappd.set", "value": "dev"}],
        "metrics": [{"key": "routing_accuracy", "value": 0.9}],
    },
}


class Server:
    """Records every request; answers the calls mappd.eval.mlflow makes."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.experiments: dict[str, str] = {}
        self.artifacts: dict[str, bytes] = {}
        self.models: dict[str, dict[str, Any]] = {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:  # noqa: PLR0911 - one per call
        path = request.url.path
        body: dict[str, Any] = (
            json.loads(request.content)
            if request.method in ("POST", "PATCH") and request.content
            else {}
        )
        self.requests.append((request.method, path, body))
        if path.endswith("/experiments/get-by-name"):
            name = request.url.params["experiment_name"]
            if name not in self.experiments:
                return httpx2.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})
            experiment = {"experiment_id": self.experiments[name]}
            return httpx2.Response(200, json={"experiment": experiment})
        if path.endswith("/experiments/create"):
            self.experiments[body["name"]] = "7"
            return httpx2.Response(200, json={"experiment_id": "7"})
        if path.endswith(("/runs/create", "/runs/get")):
            return httpx2.Response(200, json={"run": RUN})
        if path.endswith("/runs/search"):
            return httpx2.Response(200, json={"runs": [RUN]})
        if path.endswith("/logged-models/search"):
            models = [
                m
                for m in self.models.values()
                if f"name = '{m['info']['name']}'" in body.get("filter", "")
                and f"tags.`mappd.version` = '{m['version']}'" in body.get("filter", "")
            ]
            return httpx2.Response(200, json={"models": models})
        if path.endswith("/logged-models"):
            model_id = f"m-{len(self.models) + 1}"
            info = {
                "model_id": model_id,
                "name": body["name"],
                "tags": body.get("tags", []),
            }
            self.models[model_id] = {"info": info, "version": body["tags"][0]["value"]}
            return httpx2.Response(200, json={"model": {"info": info}})
        if "/logged-models/" in path and request.method == "PATCH":
            return httpx2.Response(200, json={"model": {"info": {}}})
        if "/mlflow-artifacts/artifacts/" in path:
            key = path.split("/mlflow-artifacts/artifacts/", 1)[1]
            if request.method == "PUT":
                self.artifacts[key] = request.content
                return httpx2.Response(200, json={})
            if key in self.artifacts:
                return httpx2.Response(200, content=self.artifacts[key])
            return httpx2.Response(404)
        return httpx2.Response(200, json={})

    def batches(self) -> list[dict[str, Any]]:
        """The bodies of every log-batch call."""
        return [b for _m, p, b in self.requests if p.endswith("/runs/log-batch")]
