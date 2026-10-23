"""The Operations view: cases waiting for a person, stuck cases, and trouble in the outbox.

Only what the app's own tables know: eval
metrics, LLM errors and failed workflows live in MLflow and `/workflows`. A process declares
where its cases are with `Process(cases=Cases(...))` and appears here without core code.

A case's age in a status is read from its `updated_at`: the time of its last change, which
every guarded transition sets.

Example:
    >>> Cases(
    ...     table=TICKETS,
    ...     link="/support/tickets/{key}",
    ...     waiting=("awaiting_approval", "escalated"),
    ...     passing={"new": timedelta(minutes=10), "approved": timedelta(minutes=10)},
    ... )
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa
from pydantic import BaseModel, Field

from mappd import events
from mappd.db import Connection, connect, run
from mappd.db.tables import ACTIONS

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

STUCK_LIMIT = 50


@dataclass(frozen=True)
class Cases:
    """Where a process keeps its cases, and which statuses a person or time should end.

    `waiting` statuses wait for a person (an approval, a blocked job); `also_waiting`, a
    condition on `table`, adds cases that wait for one in another way (a proposal to decide,
    a deadline passed). The process's own queue should count the same. `passing` statuses
    should end on their own within the given time (a ticket being drafted, a reply being
    sent); a case still there after it is stuck. `link` is the case's page, `{key}` filled
    from the `key` column.
    """

    table: sa.Table
    link: str
    waiting: tuple[str, ...] = ()
    also_waiting: sa.ColumnElement[bool] | None = None
    passing: Mapping[str, timedelta] = field(default_factory=dict)
    status: str = "status"
    key: str = "external_id"
    changed: str = "updated_at"

    def __post_init__(self) -> None:
        for column in (self.status, self.key, self.changed):
            if column not in self.table.c:
                raise ValueError(f"Cases: {self.table.name} has no column {column!r}")
        if "{key}" not in self.link:
            raise ValueError(f"Cases: link {self.link!r} needs a {{key}} placeholder")


class StuckCase(BaseModel):
    """A case in a passing status longer than it should be."""

    key: str
    status: str
    since: datetime
    link: str


class ProcessOps(BaseModel):
    """One process's cases as operations sees them."""

    process: str
    waiting: int
    oldest_waiting: datetime | None
    stuck: int
    stuck_cases: list[StuckCase]


class OutboxOps(BaseModel):
    """Actions a person should look at, across processes."""

    unknown: int
    failed: int
    webhooks_redelivering: int
    webhooks_given_up: int


class WorkflowOps(BaseModel):
    """Durable workflows that need a person: failed ones (last 24 h) and stranded ones."""

    failed: int
    stranded: int


class OpsReport(BaseModel):
    """The Operations view."""

    processes: list[ProcessOps]
    outbox: OutboxOps
    workflows: WorkflowOps | None = Field(
        default=None, description="filled by the app (the system database), not by `report`"
    )


def _any_status(column: sa.ColumnElement[str], statuses: Iterable[str]) -> sa.ColumnElement[bool]:
    # not IN: mappd.db compiles once, and an expanding IN leaves a POSTCOMPILE placeholder
    return sa.or_(sa.false(), *(column == s for s in statuses))


def _stuck(cases: Cases) -> sa.ColumnElement[bool]:
    status, changed = cases.table.c[cases.status], cases.table.c[cases.changed]
    return sa.or_(
        sa.false(),
        *(
            sa.and_(status == s, changed < sa.func.now() - sa.literal(limit, sa.Interval))
            for s, limit in cases.passing.items()
        ),
    )


def process_ops(conn: Connection, name: str, cases: Cases, limit: int = STUCK_LIMIT) -> ProcessOps:
    """Counts and the oldest stuck cases of one process."""
    t = cases.table
    status, changed = t.c[cases.status], t.c[cases.changed]
    waiting = _any_status(status, cases.waiting)
    if cases.also_waiting is not None:
        waiting = sa.or_(waiting, cases.also_waiting)
    stuck = _stuck(cases)
    counts = run(
        conn,
        sa.select(
            sa.func.count().filter(waiting).label("waiting"),
            sa.func.min(changed).filter(waiting).label("oldest_waiting"),
            sa.func.count().filter(stuck).label("stuck"),
        ).select_from(t),
    ).fetchone()
    rows = run(
        conn,
        sa.select(t.c[cases.key], status, changed).where(stuck).order_by(changed).limit(limit),
    ).fetchall()
    if counts is None:  # pragma: no cover - an aggregate always answers one row
        raise RuntimeError("the case counts returned no row")
    return ProcessOps(
        process=name,
        waiting=counts["waiting"],
        oldest_waiting=counts["oldest_waiting"],
        stuck=counts["stuck"],
        stuck_cases=[
            StuckCase(
                key=r[cases.key],
                status=r[cases.status],
                since=r[cases.changed],
                link=cases.link.format(key=r[cases.key]),
            )
            for r in rows
        ],
    )


def outbox_ops(conn: Connection) -> OutboxOps:
    """Unknown and failed actions; failing webhooks are counted apart (the sweep owns them)."""
    webhook = ACTIONS.c.kind == events.SEND_WEBHOOK
    failed = ACTIONS.c.status == "failed"
    window = sa.func.now() - sa.literal(events.REDELIVER_FOR, sa.Interval)
    row = run(
        conn,
        sa.select(
            sa.func.count().filter(ACTIONS.c.status == "unknown").label("unknown"),
            sa.func.count().filter(failed, sa.not_(webhook)).label("failed"),
            sa.func.count()
            .filter(failed, webhook, ACTIONS.c.created_at > window)
            .label("webhooks_redelivering"),
            sa.func.count()
            .filter(failed, webhook, ACTIONS.c.created_at <= window)
            .label("webhooks_given_up"),
        ).where(sa.or_(ACTIONS.c.status == "unknown", failed)),
    ).fetchone()
    if row is None:  # pragma: no cover - an aggregate always answers one row
        raise RuntimeError("the outbox counts returned no row")
    return OutboxOps.model_validate(dict(row))


def report(conn: Connection, processes: Iterable[tuple[str, Cases]]) -> OpsReport:
    """The whole view: every declaring process, then the outbox."""
    return OpsReport(
        processes=[process_ops(conn, name, cases) for name, cases in processes],
        outbox=outbox_ops(conn),
    )


async def areport(processes: Iterable[tuple[str, Cases]]) -> OpsReport:
    """`report` on a worker thread: the service call the API and the page share."""
    chosen = list(processes)

    def go() -> OpsReport:
        with connect() as conn:
            return report(conn, chosen)

    return await asyncio.to_thread(go)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def summary(r: OpsReport) -> str:
    """One line for doctor: what needs a look, or that nothing does."""
    parts = []
    if stuck := sum(p.stuck for p in r.processes):
        parts.append(_plural(stuck, "stuck case"))
    if r.outbox.unknown:
        parts.append(_plural(r.outbox.unknown, "unknown action"))
    if r.outbox.failed:
        parts.append(_plural(r.outbox.failed, "failed action"))
    if r.outbox.webhooks_given_up:
        parts.append(_plural(r.outbox.webhooks_given_up, "webhook") + " given up")
    if not parts:
        return "nothing stuck"
    return ", ".join(parts) + " — see /ops"
