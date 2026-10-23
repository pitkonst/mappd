import contextlib
from typing import TYPE_CHECKING, Any

import dbos_async
import httpx2
import psycopg
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from starlette.applications import Starlette
from starlette.routing import Mount

from mappd import durable, mcp

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pydantic_ai import Agent

    from mappd.settings import Settings

URL = "http://mcp.test/demo/mcp"


def demo_server() -> MCPServer:
    server = MCPServer("demo")

    @server.tool()
    def echo(text: str) -> dict[str, Any]:
        return {"text": text}

    @server.tool()
    def fail(text: str) -> dict[str, Any]:
        raise ToolError(f"cannot {text}")

    return server


@contextlib.asynccontextmanager
async def served() -> AsyncIterator[httpx2.AsyncBaseTransport]:
    """A transport whose requests reach an in-process demo MCP server."""
    server = demo_server()
    app = Starlette(
        routes=[
            Mount(
                "/demo",
                server.streamable_http_app(
                    stateless_http=True,
                    json_response=True,
                    transport_security=TransportSecuritySettings(
                        enable_dns_rebinding_protection=False
                    ),
                ),
            )
        ]
    )
    async with server.session_manager.run():
        yield httpx2.ASGITransport(app=app)


def test_toolset_lists_the_servers_tools() -> None:
    async def names() -> list[str]:
        async with served() as client, mcp.toolset(URL, id="demo", transport=client) as tools:
            return sorted(t.name for t in await tools.list_tools())

    assert dbos_async.run(names()) == ["echo", "fail"]


def test_call_returns_the_tools_result() -> None:
    async def call() -> dict[str, Any]:
        async with served() as client:
            return await mcp.call(URL, "echo", {"text": "hi"}, transport=client)

    assert dbos_async.run(call()) == {"text": "hi"}


def test_a_tool_error_raises_mcp_error() -> None:
    async def call() -> str:
        # Caught inside the server's context: its task group would wrap anything leaving it.
        async with served() as client:
            with pytest.raises(mcp.McpError) as raised:
                await mcp.call(URL, "fail", {"text": "go"}, transport=client)
        return str(raised.value)

    assert "cannot go" in dbos_async.run(call())


def test_an_agents_mcp_calls_are_journaled_steps(dbos_runtime: Settings) -> None:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("echo", {"text": "hi"})])
        return ModelResponse(parts=[TextPart("done")])

    @durable.workflow(name="test_mcp.run")
    async def run() -> str:
        async with served() as client:
            agent: Agent[None, str] = durable.agent(
                FunctionModel(model),
                name="test_mcp.agent",
                toolsets=[mcp.toolset(URL, id="demo", transport=client)],
            )
            result = await agent.run("go")
            return result.output

    from dbos import DBOS, SetWorkflowID  # noqa: PLC0415

    async def start() -> str:
        with SetWorkflowID("test-mcp-journal"):
            handle = await DBOS.start_workflow_async(run)
        return await handle.get_result()

    assert dbos_async.run(start()) == "done"
    with psycopg.connect(dbos_runtime.system_database_url) as conn:
        steps = [
            r[0]
            for r in conn.execute(
                "SELECT function_name FROM dbos.operation_outputs WHERE workflow_uuid = %s",
                ("test-mcp-journal",),
            ).fetchall()
        ]
    assert "test_mcp.agent__mcp_server__demo.call_tool" in steps, steps
