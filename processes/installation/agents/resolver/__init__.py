"""The resolver agent: what to check and propose when a technician's message blocks the job.

It reads the systems through MCP (the gateway multiplexes warehouse, logistics, tickets and
contacts) and may open a ticket; reserving parts and booking deliveries are proposals a
dispatcher confirms (`tools.for_resolver` leaves those tools out).
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import UsageLimits

from mappd import durable, gateway, mcp
from mappd.settings import environment

from ... import tools
from ...engine import JobView, ToolCall, tool_calls
from ...schemas import Resolution

PROMPT_FILE = Path(__file__).parent / "prompt.md"
INSTRUCTIONS = PROMPT_FILE.read_text(encoding="utf-8")
NAME = "installation.resolver"
DEFAULT_MCP_URL = "http://127.0.0.1:3000/mcp"
# A resolution takes 3-6 requests (stock per item, slots or a ticket, the answer) plus up to 3
# output retries. A model that keeps calling the same tool stops here, not at pydantic-ai's 50.
REQUEST_LIMIT = 12


def mcp_url() -> str:
    """The systems' MCP endpoint: the gateway's /mcp (`MAPPD_INSTALLATION_MCP_URL`)."""
    return environment().get("MAPPD_INSTALLATION_MCP_URL", "") or DEFAULT_MCP_URL


def message_text(job: JobView, reason: str, history: list[str]) -> str:
    """What the resolver reads."""
    lines = [
        f"Job: {job.external_id}",
        f"Site: {job.site}",
        f"Equipment: {', '.join(job.skus) or '(none listed)'}",
        f"Last step done: {job.state}",
        f"Problem: {reason}",
        *([f"Reply language: {job.language}"] if job.language else []),
        "Conversation:",
        *(f"- {line}" for line in history[-10:]),
    ]
    return "\n".join(lines)


def build(
    toolsets: Sequence[AbstractToolset[Any]], *, durable_runs: bool, model: Model | None = None
) -> Agent[JobView, Resolution]:
    """A resolver over these systems; durable (each model and tool call journaled) or plain."""
    chosen = [tools.for_resolver(t) for t in toolsets]
    options: dict[str, Any] = {
        "name": NAME,
        "deps_type": JobView,
        "output_type": Resolution,
        "instructions": INSTRUCTIONS,
        "toolsets": chosen,
        # Proposals are checked per tool: room for the model to correct itself.
        "retries": 3,
    }
    if durable_runs:
        return durable.agent(model or gateway.model("smart"), **options)
    return Agent(model or gateway.model("smart"), **options)


_production: list[Agent[JobView, Resolution]] = []


def production() -> Agent[JobView, Resolution]:
    """The durable resolver over the gateway's MCP endpoint (built once: DBOS names its steps)."""
    if not _production:
        systems = mcp.toolset(mcp_url(), id="installation.systems")
        _production.append(build([systems], durable_runs=True))
    return _production[0]


async def run(
    agent: Agent[JobView, Resolution], job: JobView, reason: str, history: list[str]
) -> tuple[Resolution, list[ToolCall]]:
    """One resolution and the system calls it made."""
    result = await agent.run(
        message_text(job, reason, history),
        deps=job,
        usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
    )
    return result.output, tool_calls(result.all_messages())


async def resolve(
    job: JobView, reason: str, history: list[str]
) -> tuple[Resolution, list[ToolCall]]:
    """The production resolution (the workflow's `resolve`)."""
    return await run(production(), job, reason, history)
