"""One mock LLM port for every process: each request goes to the mock that claims it.

The mock gateway config (deploy/agentgateway/gateway.mocks.yaml) sends every model name to a
single URL, but each process's mock understands only its own prompts. The router reads a
request's JSON body once, offers it to the mocks that declare `MockSpec.claims`, and hands the
raw request (body replayed) to the first that claims it — or to the mock without `claims`, the
fallback.

Run with: uvicorn mappd.mocks.llm_router:app (the processes under MAPPD_PROCESSES_DIR).
"""

import json
from typing import TYPE_CHECKING, Any

from mappd.process import MockSpec, load_processes
from mappd.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.types import ASGIApp, Message, Receive, Scope, Send


def build(specs: Sequence[MockSpec]) -> ASGIApp:
    """An ASGI app dispatching each request to the mock that claims it.

    Raises:
        ValueError: no mocks, or more than one without `claims` (which one is the fallback?).
    """
    fallbacks = [s for s in specs if s.claims is None]
    if not specs or len(fallbacks) > 1:
        raise ValueError("the router needs mocks and at most one without `claims`")
    fallback = fallbacks[0] if fallbacks else specs[0]
    claimants = [s for s in specs if s.claims is not None]

    async def lifespan(receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifespan(receive, send)
            return
        if scope["type"] != "http":
            await fallback.app(scope, receive, send)
            return
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        target = _choose(claimants, fallback, scope["path"], body)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await target.app(scope, replay, send)

    return app


def _choose(claimants: list[MockSpec], fallback: MockSpec, path: str, body: bytes) -> MockSpec:
    try:
        data: Any = json.loads(body) if body else None
    except json.JSONDecodeError:
        data = None
    offered = data if isinstance(data, dict) else {"__path__": path}
    for spec in claimants:
        if spec.claims is not None and spec.claims(offered):
            return spec
    return fallback


def __getattr__(name: str) -> ASGIApp:
    """`app`: built on first access from the registered processes' `llm` mocks."""
    if name != "app":
        raise AttributeError(name)
    procs = load_processes(Settings.from_env().processes_dir)
    return build([p.mocks["llm"] for p in procs if "llm" in p.mocks])
