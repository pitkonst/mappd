"""DBOS lifecycle for the app: configure, launch, and drain on shutdown.

DBOS is a library inside the app process; its state lives in the system database. The
DBOS-specific calls of the core sit here (and in the CLI's workflow commands), so moving to
another engine is a bounded change.
"""

import asyncio
import logging
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Protocol

from dbos import DBOS, DBOSConfig

from mappd import telemetry, workflow_stats, workflows
from mappd.db import transaction

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

    from fastapi import FastAPI

    from mappd.process import Schedule
    from mappd.settings import Settings

POLL_SECONDS = 0.2
# DBOS startup lines that do not apply here: the hosted Conductor (MAPP-D has /workflows, and
# Conductor is not OSI-licensed), and OTLP logs (MAPP-D exports traces only).
DBOS_NOISE = (
    "To view and manage workflows, connect to DBOS Conductor",
    "OTLP is enabled but logger provider not set",
)

log = logging.getLogger(__name__)


class QuietDbos(logging.Filter):
    """Drops the `DBOS_NOISE` lines from DBOS's logger."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Keep every record that does not start with a `DBOS_NOISE` line."""
        return not record.getMessage().startswith(DBOS_NOISE)


class Shutdown(Protocol):
    """Anything with a shutdown(), such as an OpenTelemetry TracerProvider."""

    def shutdown(self) -> object:
        """Flush and release resources."""
        ...


def configure(settings: Settings, *, executor_id: str | None = None) -> DBOS:
    """Create the DBOS instance for this application version. Call once, before launch.

    `executor_id` separates a short-lived executor (the CLI) from the app's: DBOS recovers the
    PENDING workflows of its own executor ID on launch.
    """
    config: DBOSConfig = {
        "name": settings.app_name,
        "system_database_url": settings.system_database_url,
        "application_version": settings.app_version,
        # DBOS workflow/step spans go to the global tracer provider, which
        # telemetry.setup (called first, in the lifespan) points at MLflow.
        "enable_otlp": True,
    }
    if executor_id is not None:
        config["executor_id"] = executor_id
    dbos_log = logging.getLogger("dbos")
    if not any(isinstance(f, QuietDbos) for f in dbos_log.filters):
        dbos_log.addFilter(QuietDbos())
    dbos = DBOS(config=config)
    # The app-DB datasource must exist before launch (transaction steps use it).
    transaction.open(settings)
    return dbos


async def pending_count(settings: Settings) -> int:
    """Number of PENDING workflows of this application version."""
    active = await DBOS.list_workflows_async(
        status="PENDING", app_version=settings.app_version, load_input=False, load_output=False
    )
    return len(active)


async def wait_for_idle(
    pending: Callable[[], Awaitable[int]], budget: float, poll: float = POLL_SECONDS
) -> bool:
    """Poll `pending` until it reports zero or `budget` seconds pass; True if it reached zero."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while await pending() > 0:
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(poll)
    return True


async def drain(settings: Settings) -> bool:
    """Let this version's in-flight workflows finish, then destroy DBOS.

    The stock `DBOS.destroy(workflow_completion_timeout_sec=...)` blocks the event loop the async
    workflows run on, so they cannot finish and the drain just times out.
    Waiting asynchronously first lets them complete.

    Returns:
        True when every workflow finished within MAPPD_DRAIN_SECONDS.
    """
    drained = await wait_for_idle(
        lambda: pending_count(settings), settings.drain_seconds, POLL_SECONDS
    )
    if not drained:
        log.warning(
            "drain: workflows of version %s still pending after %ss; "
            "the next version recovers them with `mappd workflow recover`",
            settings.app_version,
            settings.drain_seconds,
        )
    DBOS.destroy()
    return drained


def lifespan(
    settings: Settings,
    telemetry_setup: Callable[[Settings], Shutdown] = telemetry.setup,
    schedules: Sequence[Schedule] = (),
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """FastAPI lifespan: start telemetry and DBOS, apply the schedules; drain, then flush."""

    @asynccontextmanager
    async def run(_: FastAPI) -> AsyncIterator[None]:
        provider = telemetry_setup(settings)
        configure(settings)
        DBOS.launch()
        # The running app is the deployed version: `create_application_version` is
        # `ON CONFLICT DO NOTHING`, so without this a rollback would leave DBOS polling for
        # the newer version that is no longer running.
        await DBOS.set_latest_application_version_async(settings.app_version)
        # Rows in the system database: created after launch, updated on deploy, and deleted
        # once no longer declared (a schedule DBOS keeps would go on firing).
        if schedules:
            await DBOS.apply_schedules_async(
                [
                    {
                        "schedule_name": s.name,
                        "workflow_fn": s.workflow,
                        "schedule": s.cron,
                        "context": s.context,
                    }
                    for s in schedules
                ]
            )
        declared = {s.name for s in schedules}
        for stored in await DBOS.list_schedules_async():
            if stored["schedule_name"] not in declared:
                await DBOS.delete_schedule_async(stored["schedule_name"])
        try:
            yield
        finally:
            try:
                await drain(settings)
            finally:
                workflows.close_shared()
                workflow_stats.close()
                provider.shutdown()

    return run
