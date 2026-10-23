"""The support drafter agent and its read-only knowledge-base search tool."""

import asyncio
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from mappd import db, durable, gateway

from ...db import KB
from ...schemas import EMBED_DIM, Draft

PROMPT_FILE = Path(__file__).parent / "prompt.md"
INSTRUCTIONS = PROMPT_FILE.read_text(encoding="utf-8")

drafter = durable.agent(
    gateway.model("smart"),
    output_type=Draft,
    instructions=INSTRUCTIONS,
)


@drafter.tool_plain
async def search_kb(query: str) -> str:
    """Search the knowledge base; returns up to three articles as `id: title — body` lines."""
    rows = await asyncio.to_thread(_nearest, await embed(query))
    return "\n".join(f"{r['id']}: {r['title']} — {r['body']}" for r in rows)


async def embed(text: str) -> list[float]:
    """An embedding through the gateway, at the knowledge base's dimension."""
    return await gateway.embed(text, dimensions=EMBED_DIM)


def _nearest(vector: list[float]) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = db.run(
            conn,
            sa.select(KB.c.id, KB.c.title, KB.c.body)
            .order_by(KB.c.embedding.cosine_distance(str(vector)))
            .limit(3),
        ).fetchall()
    return [dict(r) for r in rows]
