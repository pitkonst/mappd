from pathlib import Path

import pytest
import sqlalchemy as sa
from dbos_async import run as arun

from mappd.db import connect, run
from mappd.db.migrate import upgrade
from mappd.settings import Settings
from processes.support.data import seed
from processes.support.db import KB

PROCESSES = Path(__file__).parents[2]


async def zeros(_text: str) -> list[float]:
    return [0.0] * 64


def test_seed_updates_articles_whose_text_changed(
    fresh_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(database_url=fresh_database_url, processes_dir=PROCESSES)
    upgrade(settings)
    monkeypatch.setenv("MAPPD_DATABASE_URL", fresh_database_url)
    monkeypatch.setattr(seed, "embed", zeros)
    with connect(settings) as conn:
        run(
            conn,
            KB.insert().values(
                id="kb-106", title="old", body="an old body", embedding=str([1.0] * 64)
            ),
        )
    assert arun(seed.seed()) == 20
    with connect(settings) as conn:
        row = run(conn, sa.select(KB.c.title, KB.c.body).where(KB.c.id == "kb-106")).fetchone()
    assert row is not None
    assert "Network Recorder NVR16" in row["body"]
    assert row["title"] == "Recorder storage full"


def test_load_articles_is_an_async_generator_yielding_kb_articles() -> None:
    async def collect() -> list[tuple[str, str, str]]:
        return [(a.id, a.title, a.body) async for a in seed.load_articles()]

    articles = arun(collect())
    assert len(articles) == 20
    assert any(article_id == "kb-106" for article_id, _, _ in articles)


def test_seed_processes_articles_in_batches(
    fresh_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(database_url=fresh_database_url, processes_dir=PROCESSES)
    upgrade(settings)
    monkeypatch.setenv("MAPPD_DATABASE_URL", fresh_database_url)
    monkeypatch.setattr(seed, "embed", zeros)
    batch_sizes: list[int] = []
    real_upsert = seed._upsert_batch

    def record_batch(conn: object, articles: list[object], vectors: list[list[float]]) -> None:
        batch_sizes.append(len(articles))
        real_upsert(conn, articles, vectors)  # type: ignore[arg-type]

    monkeypatch.setattr(seed, "_upsert_batch", record_batch)
    assert arun(seed.seed(batch_size=6)) == 20
    assert batch_sizes == [6, 6, 6, 2]
