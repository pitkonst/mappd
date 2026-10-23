"""The systems' tools as the resolver sees them: reads and tickets, never commitments.

`for_resolver` wraps a toolset (the MCP servers through the gateway, or the in-process systems
in evals) so that the confirmed tools are absent — the resolver can only propose them — and the
idempotency `key` of a mutating tool comes from the turn (`<job>:<turn>:<tool>`), not from the
model: the model never sees the parameter.
"""

from dataclasses import dataclass, field, replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.toolsets import AbstractToolset, WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from .engine import JobView

KEY = "key"
# The only tools the resolver may call (AGENTS.md, rule 6): reads, and an internal ticket.
# An allow-list, so a new mutating tool or a prefixed name never reaches the model.
RESOLVER_TOOLS = frozenset({"check_stock", "delivery_slots", "open_ticket", "site_contact"})


@dataclass
class KeyedToolset(WrapperToolset[JobView]):
    """Hides `key` from the model and fills it from the run's job and turn."""

    keyed: set[str] = field(default_factory=set)

    async def get_tools(self, ctx: RunContext[JobView]) -> dict[str, ToolsetTool[JobView]]:
        """The wrapped tools without the `key` parameter in their schemas."""
        tools = await super().get_tools(ctx)
        out: dict[str, ToolsetTool[JobView]] = {}
        for name, tool in tools.items():
            schema = tool.tool_def.parameters_json_schema
            properties = schema.get("properties", {})
            if KEY not in properties:
                out[name] = tool
                continue
            self.keyed.add(name)
            schema = {
                **schema,
                "properties": {k: v for k, v in properties.items() if k != KEY},
                "required": [r for r in schema.get("required", []) if r != KEY],
            }
            out[name] = replace(
                tool, tool_def=replace(tool.tool_def, parameters_json_schema=schema)
            )
        return out

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[JobView],
        tool: ToolsetTool[JobView],
    ) -> Any:
        """Call the tool, with the turn's key when it takes one."""
        if name in self.keyed:
            tool_args = {**tool_args, KEY: f"{ctx.deps.external_id}:{ctx.deps.turn_key}:{name}"}
        return await super().call_tool(name, tool_args, ctx, tool)


def for_resolver(toolset: AbstractToolset[Any]) -> AbstractToolset[JobView]:
    """The resolver's view of a systems toolset: allowed tools only, keys from the turn."""
    allowed = toolset.filtered(lambda _ctx, tool: tool.name in RESOLVER_TOOLS)
    return KeyedToolset(allowed)
