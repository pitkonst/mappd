"""The app database. Business state lives here, not in DBOS.

DBOS holds only workflows in flight for seconds; a case waiting for a person is a row.

Reads and one-shot writes are async (`aconnect`/`arun`): they carry no transaction of their
own, so they can be awaited straight from a workflow or a handler. Durable writes inside a
workflow are different — they must share the DBOS datasource's sync transaction, so
`transaction` steps take a sync `Connection` and callers dispatch them via asyncio.to_thread.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from sqlalchemy.dialects import postgresql

from mappd.settings import Settings

__all__ = ["AsyncConnection", "Connection", "aconnect", "arun", "connect", "run", "tx"]

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

if TYPE_CHECKING:
    from sqlalchemy import ClauseElement
    from sqlalchemy.engine import Dialect

type Connection = psycopg.Connection[DictRow]
type AsyncConnection = psycopg.AsyncConnection[DictRow]

# psycopg 3 binds %(name)s parameters; compile every Core statement with this dialect.
DIALECT: Dialect = postgresql.dialect(  # type: ignore[no-untyped-call]
    paramstyle="pyformat"
)


def connect(settings: Settings | None = None) -> Connection:
    """Open an autocommit connection to the app database that returns rows as dicts."""
    url = (settings or Settings.from_env()).database_url
    return psycopg.connect(url, row_factory=dict_row, autocommit=True)


def _compiled(
    stmt: ClauseElement, params: Mapping[str, object] | None
) -> tuple[str, dict[str, object]]:
    """Compile `stmt` to SQL and parameters; `params` fills free `bindparam()`s."""
    compiled = stmt.compile(dialect=DIALECT)
    merged = {**compiled.params, **(params or {})}
    # psycopg adapts Jsonb explicitly, not plain dicts; JSONB-typed binds get dict values.
    adapted = {k: Jsonb(v) if isinstance(v, dict) else v for k, v in merged.items()}
    return str(compiled), adapted


def run(
    conn: Connection, stmt: ClauseElement, params: Mapping[str, object] | None = None
) -> psycopg.Cursor[DictRow]:
    """Execute a SQLAlchemy Core statement on `conn`; `params` fills free bindparams.

    Values embedded by `.values()` ship with the statement; `bindparam()` placeholders
    without a value come from `params`.
    """
    # A dict-row cursor of our own: the connection may belong to SQLAlchemy (a
    # transaction step's), so its row_factory must not be touched.
    return conn.cursor(row_factory=dict_row).execute(*_compiled(stmt, params))


@asynccontextmanager
async def aconnect(settings: Settings | None = None) -> AsyncIterator[AsyncConnection]:
    """Open an autocommit async connection to the app database that returns rows as dicts."""
    url = (settings or Settings.from_env()).database_url
    async with await psycopg.AsyncConnection.connect(
        url, row_factory=dict_row, autocommit=True
    ) as conn:
        yield conn


async def arun(
    conn: AsyncConnection, stmt: ClauseElement, params: Mapping[str, object] | None = None
) -> psycopg.AsyncCursor[DictRow]:
    """The async counterpart of `run`, for plain reads and writes outside transactions."""
    return await conn.cursor(row_factory=dict_row).execute(*_compiled(stmt, params))


async def tx[**P, R](func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run a sync durable step — a `transaction` write or an `action` — off the event loop.

    Sync steps must not run on the event loop a workflow shares with every request; this
    is `asyncio.to_thread` named for the only kind of call a workflow makes with it.
    """
    return await asyncio.to_thread(func, *args, **kwargs)
