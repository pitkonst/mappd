"""A fake agentgateway: OpenAI-compatible endpoints on a background thread.

GET /v1/models lists MODELS, POST /v1/chat/completions answers "pong" for them and 404 for
anything else, GET /health answers 200 (it doubles as MLflow's health endpoint).
"""

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

MODELS = ("embed", "fast", "smart")


@dataclass(frozen=True)
class Gateway:
    url: str
    root: str
    requests: list[dict[str, Any]] = field(default_factory=list)


def completion(model: str, content: str) -> dict[str, Any]:
    return {
        "id": "c-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


class _Server(ThreadingHTTPServer):
    requests: list[dict[str, Any]]


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/v1/models":
            data = [{"id": m, "object": "model", "owned_by": "fake"} for m in MODELS]
            self._send(200, {"object": "list", "data": data})
        elif self.path == "/health":
            self._send(200, {"status": "OK"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        server = self.server
        assert isinstance(server, _Server)
        server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        model = body.get("model")
        if self.path == "/v1/chat/completions" and model in MODELS:
            self._send(200, completion(model, "pong"))
        else:
            self._send(404, {"error": {"message": f"unknown model {model!r}"}})

    def _send(self, status: int, payload: object) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass


@contextmanager
def running() -> Iterator[Gateway]:
    server = _Server(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    root = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield Gateway(url=f"{root}/v1", root=root, requests=server.requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
