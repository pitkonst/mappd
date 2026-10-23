"""Transactional app-DB writes inside workflows: the write and its checkpoint commit together.

A `@transaction("<process>.<function>")` step runs its body inside a DBOS datasource
transaction on the application database: the business write and the step's
`dbos.datasource_outputs` row commit in one Postgres transaction, so a crash between the
two cannot repeat the write. Inside a workflow the
call is a step named `<process>.<function>`; outside one it is simply a transaction.

The datasource is created in `open` — before `DBOS.launch()`, which refuses late
datasources — and its tables are created by `migrate`, called from `mappd db migrate`.
"""

import asyncio
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, cast, overload

from dbos import SQLAlchemyDatasource
from dbos._registrations import set_dbos_func_name

from mappd.db import connect
from mappd.db.migrate import sqlalchemy_url
from mappd.naming import step_name

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator

    from dbos._datasource import DatasourceOptions

    from mappd.db import Connection
    from mappd.settings import Settings

TRANSACTIONS: set[str] = set()
"""Every transaction step's name, registered by the decorator (the dashboard's step kinds)."""

_DS: SQLAlchemyDatasource | None = None
_DS_URL: str | None = None


def open(settings: Settings) -> None:  # noqa: A001 - public name per the module's API
    """Create the datasource on the app database. Idempotent per process and database.

    Called from `runtime.configure`: DBOS refuses datasources created after launch, and a
    later configure with the same database reuses the datasource (the DBOS registry keeps
    it across `DBOS.destroy()`).
    """
    global _DS, _DS_URL  # noqa: PLW0603 - the process's single datasource
    url = sqlalchemy_url(settings.database_url)
    if _DS is not None and url == _DS_URL:
        return
    _DS = SQLAlchemyDatasource.create(url, run_migrations=False)
    _DS_URL = url


def migrate(settings: Settings) -> None:
    """Create or migrate the datasource tables (dbos.datasource_outputs) in the app DB."""
    SQLAlchemyDatasource.migrate(sqlalchemy_url(settings.database_url))


def _datasource() -> SQLAlchemyDatasource:
    if _DS is None:
        raise RuntimeError(
            "no datasource: mappd.db.transaction.open(settings) must run first "
            "(runtime.configure does it before DBOS.launch)"
        )
    return _DS


@contextmanager
def atomic() -> Iterator[Connection]:
    """Run a block of statements in one transaction on the app database.

    Inside a `transaction` step it is a SAVEPOINT of the step's transaction — a raised
    error rolls the block back, the step commits the rest. Everywhere else it opens a
    transaction on the datasource engine (a plain one on the app DB before `open`).

    It is not a checkpoint: durable writes in a workflow stay `transaction` steps —
    the function boundary is what makes a write replayable by name.
    """
    ds = _DS
    if ds is not None:
        try:
            session = ds.sql_session()  # the current transaction step's session
        except AssertionError:
            pass
        else:
            conn = cast("Connection", session.connection().connection.driver_connection)
            with conn.transaction():  # SAVEPOINT of the step's transaction
                yield conn
            return
        with ds.sessionmaker(bind=ds.engine) as session, session.begin():
            yield cast("Connection", session.connection().connection.driver_connection)
        return
    with connect() as conn, conn.transaction():
        yield conn


@overload
def transaction[**P, R](
    func: Callable[Concatenate[Connection, P], R], /
) -> Callable[P, Coroutine[Any, Any, R]]: ...


@overload
def transaction[**P, R](
    name: str, /
) -> Callable[[Callable[Concatenate[Connection, P], R]], Callable[P, Coroutine[Any, Any, R]]]: ...


def transaction[**P, R](
    arg: str | Callable[Concatenate[Connection, P], R],
) -> (
    Callable[P, Coroutine[Any, Any, R]]
    | Callable[[Callable[Concatenate[Connection, P], R]], Callable[P, Coroutine[Any, Any, R]]]
):
    """Decorate a write as a datasource transaction step; callers `await` it.

    Bare — `@transaction` — the step is named "<process>.<function>", derived from the
    function's module (mappd.naming.step_name). `@transaction("name")` pins a name: the
    escape hatch for a deliberate rename that must keep journal compatibility.

    The decorated function takes a `mappd.db.Connection` as its first parameter; callers
    omit it — the wrapper injects the psycopg connection of the DBOS-managed session, so
    `mappd.db.run` and `mappd.locking.update_if_version` work unchanged on it. The sync
    body runs on a worker thread: awaiting a transaction step never blocks the event loop.
    """

    def decorator(
        func: Callable[Concatenate[Connection, P], R],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        name = arg if isinstance(arg, str) else step_name(func)
        TRANSACTIONS.add(name)
        options: DatasourceOptions = {"name": name, "isolation_level": "READ COMMITTED"}

        def call(*args: P.args, **kwargs: P.kwargs) -> R:
            ds = _datasource()

            def body(*args: P.args, **kwargs: P.kwargs) -> R:
                # The session's raw psycopg connection: business write and checkpoint
                # share this transaction.
                conn = cast(
                    "Connection",
                    ds.sql_session().connection().connection.driver_connection,
                )
                return func(conn, *args, **kwargs)

            return ds.run_tx_step(options, body, *args, **kwargs)

        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return await asyncio.to_thread(call, *args, **kwargs)

        set_dbos_func_name(wrapper, name)
        return wrapper

    return decorator(arg) if callable(arg) else decorator
