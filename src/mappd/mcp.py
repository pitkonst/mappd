"""MCP tools through the gateway: a toolset for agents and one direct call for durable actions.

agentgateway multiplexes a process's MCP servers into one virtual server at its `/mcp` path
(`mcp:` in the gateway config, `prefixMode: never`), so the app talks to one URL and the tool
names stay as the servers declare them. Requests use the instrumented gateway client: tool
calls join the workflow's trace.

Example:
    >>> tools = toolset(url, id="installation.systems")  # Agent(..., toolsets=[tools])
    >>> await call(url, "reserve_parts", {"job": "j-1", "skus": ["CTL-H400"], "key": "p-1"})
"""

from typing import Any

import httpx2
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import MCPError as ProtocolError
from pydantic_ai.mcp import MCPToolset

from . import telemetry
from .gateway import GATEWAY_TIMEOUT


class McpError(RuntimeError):
    """An MCP tool call failed (the tool raised, or the server refused the call)."""


class McpToolError(McpError):
    """The tool ran and reported an error: a final answer, not a transport failure."""


def _transport(url: str, transport: httpx2.AsyncBaseTransport | None) -> StreamableHttpTransport:
    """A streamable-HTTP transport opening a fresh traced client per connection.

    The MCP client enters its HTTP client's context on every connect, and a client opens only
    once, so a long-lived toolset needs a factory rather than one shared client.
    """

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        follow_redirects: bool = True,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=telemetry.TracedTransport(transport),
            headers=headers,
            timeout=timeout or GATEWAY_TIMEOUT,
            auth=auth,
            follow_redirects=follow_redirects,
        )

    return StreamableHttpTransport(url, httpx_client_factory=factory)


def toolset(
    url: str,
    *,
    id: str,  # noqa: A002 - Pydantic AI's name
    transport: httpx2.AsyncBaseTransport | None = None,
) -> MCPToolset[Any]:
    """The MCP server at `url` as a Pydantic AI toolset.

    Args:
        url: The streamable-HTTP MCP endpoint (the gateway's `/mcp`).
        id: A stable toolset id; DBOS names the journaled tool-call steps after it.
        transport: The HTTP transport under the traced client (tests pass an ASGI transport).
    """
    return MCPToolset(_transport(url, transport), id=id)


async def call(
    url: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Call one tool and return its structured result.

    Raises:
        McpError: the tool raised or the server refused the call.
    """
    tools: MCPToolset[Any] = MCPToolset(_transport(url, transport), tool_error_behavior="error")
    try:
        result = await tools.direct_call_tool(tool, arguments)
    except ToolError as e:
        raise McpToolError(f"{tool}: {e}") from e
    except ProtocolError as e:
        raise McpError(f"{tool}: {e}") from e
    if not isinstance(result, dict):
        raise McpError(f"{tool}: expected a JSON object, got {type(result).__name__}")
    return result
