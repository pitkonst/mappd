"""Counts for the workflow dashboard: the only code that reads a DBOS table.

DBOS's public client lists workflows but cannot count them, so the tiles and the queue line come
from one read-only `GROUP BY` on `dbos.workflow_status` (a documented system table). Only the
columns below are declared; `tests/test_workflow_stats.py` selects them against a real DBOS
schema, so an upgrade that changes the table fails CI rather than the dashboard.
"""

import threading
import time
from datetime import timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa

from mappd.db.migrate import sqlalchemy_url
from mappd.workflow_view import (
    CLI_EXECUTOR_PREFIX,
    IN_FLIGHT,
    QueueDepth,
    WorkflowStats,
    process_of,
)

if TYPE_CHECKING:
    from datetime import datetime

    from mappd.settings import Settings

WINDOWS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}
INTERNAL_QUEUE_PREFIX = "_dbos"

STATUS = sa.table(
    "workflow_status",
    sa.column("workflow_uuid", sa.Text),
    sa.column("status", sa.Text),
    sa.column("name", sa.Text),
    sa.column("queue_name", sa.Text),
    sa.column("created_at", sa.BigInteger),
    sa.column("application_name", sa.Text),
    sa.column("application_version", sa.Text),
    sa.column("executor_id", sa.Text),
    schema="dbos",
)

_ENGINES: dict[str, sa.Engine] = {}
_ENGINES_LOCK = threading.Lock()  # the app calls in from several worker threads at once


def _engine(settings: Settings) -> sa.Engine:
    url = sqlalchemy_url(settings.system_database_url)
    with _ENGINES_LOCK:
        if url not in _ENGINES:
            _ENGINES[url] = sa.create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2)
        return _ENGINES[url]


def close() -> None:
    """Dispose the cached engines (the app's shutdown)."""
    with _ENGINES_LOCK:
        while _ENGINES:
            _ENGINES.popitem()[1].dispose()


def _mine(settings: Settings) -> sa.ColumnElement[bool]:
    return STATUS.c.application_name == settings.app_name


def stats(
    settings: Settings, *, since: datetime, stranded: int, only: list[str] | None = None
) -> WorkflowStats:
    """Counts by status and name since `since`, and the depth of every user queue.

    `stranded` comes from the caller (`mappd.workflows.stats`), which owns that definition.
    `only` narrows the counts to those workflow names (a process, scheduled ticks hidden).
    """
    since_ms = int(since.timestamp() * 1000)
    recent = sa.and_(_mine(settings), STATUS.c.created_at >= since_ms)
    if only is not None:
        recent = sa.and_(recent, STATUS.c.name.in_(only))
    by_status = sa.select(STATUS.c.status, sa.func.count()).where(recent).group_by(STATUS.c.status)
    by_name = sa.select(STATUS.c.name, sa.func.count()).where(recent).group_by(STATUS.c.name)
    waiting = sa.func.count().filter(STATUS.c.status == "ENQUEUED")
    running = sa.func.count().filter(STATUS.c.status == "PENDING")
    queues = (
        sa.select(STATUS.c.queue_name, waiting, running)
        .where(
            _mine(settings),
            STATUS.c.status.in_(IN_FLIGHT),
            STATUS.c.queue_name.is_not(None),
            sa.not_(STATUS.c.queue_name.startswith(INTERNAL_QUEUE_PREFIX)),
        )
        .group_by(STATUS.c.queue_name)
        .order_by(STATUS.c.queue_name)
    )
    with _engine(settings).connect() as conn:
        statuses = {str(s): int(n) for s, n in conn.execute(by_status).all()}
        counts = {str(s): int(n) for s, n in conn.execute(by_name).all() if s is not None}
        depth = [
            QueueDepth(name=str(q), waiting=int(w), running=int(r))
            for q, w, r in conn.execute(queues).all()
        ]
    return WorkflowStats(
        since=since,
        by_status=statuses,
        stranded=stranded,
        by_name=counts,
        queues=depth,
        processes=sorted({p for n in names(settings) if (p := process_of(n))}),
    )


def stranded(settings: Settings, *, current: str, abandoned_after: float) -> int:
    """How many workflows no running executor will finish, counted in SQL.

    The same rule as `mappd.workflows._is_stranded` (a pg test pins the two together): in
    flight on another version than `current`, or PENDING under a `mappd process run` executor
    older than `abandoned_after` seconds. Counting here keeps a backlog of thousands of rows
    from being fetched on every dashboard refresh.
    """
    cutoff_ms = int((time.time() - abandoned_after) * 1000)
    abandoned = sa.and_(
        STATUS.c.status == "PENDING",
        STATUS.c.executor_id.startswith(CLI_EXECUTOR_PREFIX, autoescape=True),
        sa.func.coalesce(STATUS.c.created_at, 0) <= cutoff_ms,
    )
    query = sa.select(sa.func.count()).where(
        _mine(settings),
        STATUS.c.status.in_(IN_FLIGHT),
        sa.or_(STATUS.c.application_version.is_distinct_from(current), abandoned),
    )
    with _engine(settings).connect() as conn:
        return int(conn.execute(query).scalar_one())


SCHEDULED_PREFIX = "sched-"  # DBOS names a scheduled run "sched-<schedule>-<time>"


def scheduled_names(settings: Settings) -> set[str]:
    """The workflows this application runs on a schedule (a mail poll, a deadline check)."""
    query = (
        sa.select(STATUS.c.name)
        .where(_mine(settings), STATUS.c.workflow_uuid.startswith(SCHEDULED_PREFIX))
        .distinct()
    )
    with _engine(settings).connect() as conn:
        return {str(n) for (n,) in conn.execute(query).all() if n is not None}


def names(settings: Settings) -> list[str]:
    """Every workflow name this application has run, sorted."""
    query = sa.select(STATUS.c.name).where(_mine(settings), STATUS.c.name.is_not(None)).distinct()
    with _engine(settings).connect() as conn:
        # Sorted here, not by the database's collation: the same order in every locale.
        return sorted(str(n) for (n,) in conn.execute(query).all())
