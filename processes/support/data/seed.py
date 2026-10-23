"""Seed the knowledge base with articles; embeddings go through the gateway.

Run with: mappd process seed support
"""

import asyncio
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mappd import db
from mappd.data import world

from ..agents.drafter import embed
from ..db import KB

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

BATCH_SIZE = 64


async def seed(batch_size: int = BATCH_SIZE) -> int:
    """Stream `load_articles()` in batches, embed and upsert each batch, and return total rows."""
    with db.connect() as conn:
        async for batch in _batches(load_articles(), batch_size):
            vectors = [await embed(f"{a.title} {a.body}") for a in batch]
            _upsert_batch(conn, batch, vectors)
        row = db.run(conn, sa.select(sa.func.count().label("n")).select_from(KB)).fetchone()
    return int(row["n"]) if row else 0


async def load_articles() -> AsyncIterator[world.Article]:
    """Yield knowledge-base articles (`id`, `title`, `body`) to index into `support_kb`.

    For the demo, this yields the curated articles from `world.yaml`. In a real deployment,
    replace the body of this generator to yield articles from your own source of truth (for
    example: Markdown/JSON files in this directory, a helpdesk/wiki API such as Zendesk Guide,
    Confluence or Notion, or chunked product documentation).

    Yields:
        Articles carrying `id`, `title`, and `body` fields to embed and upsert into `support_kb`.
    """
    for article in world.load().knowledge_base:
        yield article


async def _batches(
    source: AsyncIterator[world.Article], size: int
) -> AsyncIterator[list[world.Article]]:
    batch: list[world.Article] = []
    async for article in source:
        batch.append(article)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _upsert_batch(
    conn: db.Connection,
    articles: Sequence[world.Article],
    vectors: Sequence[list[float]],
) -> None:
    rows = [
        {
            "id": article.id,
            "title": article.title,
            "body": article.body,
            "embedding": str(vector),
        }
        for article, vector in zip(articles, vectors, strict=True)
    ]
    ins = pg_insert(KB).values(rows)
    db.run(
        conn,
        ins.on_conflict_do_update(
            index_elements=[KB.c.id],
            set_={
                "title": ins.excluded.title,
                "body": ins.excluded.body,
                "embedding": ins.excluded.embedding,
            },
        ),
    )


if __name__ == "__main__":
    print(f"knowledge base: {asyncio.run(seed())} articles")
