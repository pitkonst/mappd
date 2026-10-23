"""A stand-in for the installation systems' MCP servers (`mappd mock mcp`, port 9400).

Four streamable-HTTP MCP servers over one shared `Systems`, at `/warehouse/mcp`,
`/logistics/mcp`, `/tickets/mcp` and `/contacts/mcp`; agentgateway multiplexes them into one.
Test hooks list the calls (`GET /test/calls`) and restore the world's state (`POST /test/reset`).
"""

import contextlib
import functools
import inspect
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mappd.data.world import load

from .systems import TOOLS, Facts, Systems

PORT = 9400
# A test double behind compose host names (mock-mcp, host.docker.internal): no Host checks.
OPEN = TransportSecuritySettings(enable_dns_rebinding_protection=False)

_state: dict[str, Systems] = {}


def systems() -> Systems:
    """The shared systems (the world's state until reset)."""
    if "s" not in _state:
        _state["s"] = Systems(Facts.from_world(load()))
    return _state["s"]


def reset() -> None:
    """Forget calls, reservations, bookings and tickets."""
    _state.pop("s", None)


def _server(name: str) -> MCPServer:
    server = MCPServer(name)
    for tool in TOOLS[name]:
        server.add_tool(_bind(tool), name=tool, description=getattr(Systems, tool).__doc__)
    return server


def _bind(tool: str) -> Callable[..., dict[str, Any]]:
    """A function with the Systems method's signature (no `self`) calling the shared systems.

    The MCP input schema comes from this signature.
    """
    method = getattr(Systems, tool)

    @functools.wraps(method)
    def bound(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result: dict[str, Any] = getattr(systems(), tool)(*args, **kwargs)
        return result

    sig = inspect.signature(method)
    bound.__signature__ = sig.replace(parameters=list(sig.parameters.values())[1:])  # type: ignore[attr-defined]
    return bound


@contextlib.asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[None]:
    """Run an app's MCP session managers (its lifespan; tests enter it directly)."""
    async with contextlib.AsyncExitStack() as stack:
        for server in app.state.servers.values():
            await stack.enter_async_context(server.session_manager.run())
        yield


async def _calls(_: Request) -> JSONResponse:
    return JSONResponse([{"tool": t, "arguments": a} for t, a in systems().calls])


async def _reset(_: Request) -> JSONResponse:
    reset()
    return JSONResponse({})


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with running(app):
        yield


def create_app() -> FastAPI:
    """A fresh app: an MCP session manager runs once, so each test server needs its own."""
    servers = {name: _server(name) for name in TOOLS}
    app = FastAPI(
        title="mock-mcp",
        routes=[
            Route("/test/calls", _calls),
            Route("/test/reset", _reset, methods=["POST"]),
            *(
                Mount(
                    f"/{name}",
                    server.streamable_http_app(
                        stateless_http=True, json_response=True, transport_security=OPEN
                    ),
                )
                for name, server in servers.items()
            ),
        ],
        lifespan=_lifespan,
    )
    app.state.servers = servers
    return app


app = create_app()
