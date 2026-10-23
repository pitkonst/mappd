from typing import TYPE_CHECKING

import dbos_async
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mappd.db import Connection, aconnect, arun, connect, run

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mappd.settings import Settings

TABLE = sa.Table(
    "run_demo",
    sa.MetaData(),
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("name", sa.Text),
    sa.Column("payload", sa.dialects.postgresql.JSONB),
    sa.Column("n", sa.Integer),
)


@pytest.fixture
def conn(migrated: Settings) -> Iterator[Connection]:
    with connect(migrated) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS run_demo ("
            " id bigserial PRIMARY KEY, name text NOT NULL UNIQUE,"
            " payload jsonb NOT NULL, n int NOT NULL DEFAULT 0)"
        )
        yield c


def test_insert_select_update(conn: Connection) -> None:
    run(conn, pg_insert(TABLE).values(name="a", payload={"x": 1}))
    row = run(conn, sa.select(TABLE.c.name, TABLE.c.payload).where(TABLE.c.name == "a")).fetchone()
    assert row is not None
    assert dict(row) == {"name": "a", "payload": {"x": 1}}

    run(conn, sa.update(TABLE).where(TABLE.c.name == "a").values(n=TABLE.c.n + 1))
    row = run(conn, sa.select(TABLE.c.n).where(TABLE.c.name == "a")).fetchone()
    assert row is not None
    assert row["n"] == 1


def test_params_argument_binds_free_bindparams(conn: Connection) -> None:
    run(conn, pg_insert(TABLE).values(name="b", payload={}))
    stmt = sa.select(TABLE.c.name).where(TABLE.c.n == sa.bindparam("min_n"))
    row = run(conn, stmt, {"min_n": 0}).fetchone()
    assert row is not None
    assert row["name"] == "b"


def test_on_conflict_and_returning(conn: Connection) -> None:
    first = run(
        conn,
        pg_insert(TABLE).values(name="c", payload={}).returning(TABLE.c.id),
    ).fetchone()
    again = run(
        conn,
        pg_insert(TABLE)
        .values(name="c", payload={})
        .on_conflict_do_nothing(index_elements=["name"])
        .returning(TABLE.c.id),
    ).fetchone()
    assert first is not None
    assert again is None


def test_arun_is_the_async_counterpart(migrated: Settings) -> None:
    """aconnect/arun give a workflow's plain reads an awaitable path, same dict rows."""

    async def scenario() -> dict[str, object] | None:
        async with aconnect(migrated) as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS run_demo ("
                " id bigserial PRIMARY KEY, name text NOT NULL UNIQUE,"
                " payload jsonb NOT NULL, n int NOT NULL DEFAULT 0)"
            )
            await arun(conn, pg_insert(TABLE).values(name="e", payload={"y": 2}))
            await arun(conn, sa.update(TABLE).where(TABLE.c.name == "e").values(n=TABLE.c.n + 1))
            row = await (
                await arun(conn, sa.select(TABLE.c.payload, TABLE.c.n).where(TABLE.c.name == "e"))
            ).fetchone()
        return dict(row) if row is not None else None

    assert dbos_async.run(scenario()) == {"payload": {"y": 2}, "n": 1}


def test_for_update_locks_the_row(conn: Connection) -> None:
    run(conn, pg_insert(TABLE).values(name="d", payload={}))
    with conn.transaction():
        row = run(
            conn,
            sa.select(TABLE.c.n).where(TABLE.c.name == "d").with_for_update(),
        ).fetchone()
        assert row is not None
        run(
            conn,
            sa.update(TABLE).where(TABLE.c.name == "d").values(n=row["n"] + 1),
        )
    row = run(conn, sa.select(TABLE.c.n).where(TABLE.c.name == "d")).fetchone()
    assert row is not None
    assert row["n"] == 1
