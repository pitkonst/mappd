import asyncio
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING

import dbos_async
import pytest
from dbos import DBOS
from fastapi import FastAPI

import mappd.db.transaction
from mappd import runtime, workflow_stats, workflows
from mappd.db.migrate import upgrade
from mappd.process import Schedule
from mappd.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime
    from pathlib import Path


@pytest.fixture(autouse=True)
def latest_version_noop(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mocked lifespans have no DBOS underneath; the pg test exercises the real call."""

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    if request.node.get_closest_marker("pg") is None:
        monkeypatch.setattr(DBOS, "set_latest_application_version_async", noop)


def counter(values: list[int]) -> Callable[[], Awaitable[int]]:
    remaining = list(values)

    async def pending() -> int:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return pending


def test_wait_for_idle_returns_once_nothing_is_pending() -> None:
    assert asyncio.run(runtime.wait_for_idle(counter([2, 1, 0]), budget=5, poll=0.01))


def test_wait_for_idle_gives_up_after_the_budget() -> None:
    started = time.monotonic()
    assert not asyncio.run(runtime.wait_for_idle(counter([1]), budget=0.1, poll=0.01))
    assert time.monotonic() - started < 1


def test_drain_destroys_dbos_after_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    async def pending(_: Settings) -> int:
        events.append("checked")
        return 0

    monkeypatch.setattr(runtime, "pending_count", pending)
    monkeypatch.setattr(DBOS, "destroy", lambda **_: events.append("destroyed"))
    assert asyncio.run(runtime.drain(Settings()))
    assert events == ["checked", "destroyed"]


def test_drain_warns_when_the_budget_runs_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def pending(_: Settings) -> int:
        return 1

    monkeypatch.setattr(runtime, "pending_count", pending)
    monkeypatch.setattr(runtime, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(DBOS, "destroy", lambda **_: None)
    with caplog.at_level("WARNING", logger="mappd.runtime"):
        assert not asyncio.run(runtime.drain(Settings(drain_seconds=0.05, app_version="v7")))
    assert "v7" in caplog.text
    assert "mappd workflow recover" in caplog.text


async def no_schedules(**_kw: object) -> list[dict[str, object]]:
    return []


def test_lifespan_flushes_telemetry_even_if_drain_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    flushed: list[bool] = []

    class Provider:
        def shutdown(self) -> None:
            flushed.append(True)

    async def drain(_: Settings) -> bool:
        raise RuntimeError("system database gone")

    monkeypatch.setattr(runtime, "configure", lambda _: None)
    monkeypatch.setattr(DBOS, "launch", lambda: None)
    monkeypatch.setattr(DBOS, "list_schedules_async", no_schedules)
    monkeypatch.setattr(runtime, "drain", drain)

    async def serve() -> None:
        async with runtime.lifespan(Settings(), telemetry_setup=lambda _: Provider())(FastAPI()):
            pass

    with pytest.raises(RuntimeError, match="system database gone"):
        asyncio.run(serve())
    assert flushed == [True]


def test_lifespan_order_is_telemetry_launch_serve_drain_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Provider:
        def shutdown(self) -> None:
            events.append("telemetry shutdown")

    def setup(_: Settings) -> Provider:
        events.append("telemetry")
        return Provider()

    async def drain(_: Settings) -> bool:
        events.append("drain")
        return True

    monkeypatch.setattr(runtime, "configure", lambda _: events.append("configure"))
    monkeypatch.setattr(DBOS, "launch", lambda: events.append("launch"))
    monkeypatch.setattr(DBOS, "list_schedules_async", no_schedules)
    monkeypatch.setattr(runtime, "drain", drain)

    async def serve() -> None:
        async with runtime.lifespan(Settings(), telemetry_setup=setup)(FastAPI()):
            events.append("serving")

    asyncio.run(serve())
    assert events == ["telemetry", "configure", "launch", "serving", "drain", "telemetry shutdown"]


def test_pending_count_sees_no_workflows_on_a_fresh_system_database(
    dbos_runtime: Settings,
) -> None:
    assert dbos_async.run(runtime.pending_count(dbos_runtime)) == 0


def test_lifespan_marks_the_running_version_as_dbos_latest(
    dbos_runtime: Settings, recreate_database: Callable[[str], str], tmp_path: Path
) -> None:
    # DBOS's version insert is ON CONFLICT DO NOTHING: without the lifespan's set-latest a
    # rollback (v2 deployed, then v1 again) would leave v2 latest and its polls would stop.
    # The global DBOS instance is a singleton: the session's is set aside and restored.
    DBOS.destroy()
    try:
        base = Settings(
            database_url=recreate_database("mappd_test_runtime_app"),
            system_database_url=recreate_database("mappd_test_runtime_dbos"),
            processes_dir=tmp_path,
        )
        upgrade(base)
        seen: list[str] = []

        class Provider:
            def shutdown(self) -> None:
                return None

        for version in ("v2", "v1"):
            settings = replace(base, app_version=version)

            async def serve(s: Settings = settings) -> None:
                lifespan = runtime.lifespan(s, telemetry_setup=lambda _s: Provider())
                async with lifespan(FastAPI()):
                    seen.append(DBOS.get_latest_application_version()["version_name"])

            dbos_async.run(serve())
    finally:
        runtime.configure(dbos_runtime)
        DBOS.launch()
    assert seen == ["v2", "v1"]


def test_configure_passes_name_database_version_and_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []
    opened: list[Settings] = []
    monkeypatch.setattr(runtime, "DBOS", lambda config: seen.append(dict(config)))
    monkeypatch.setattr(mappd.db.transaction, "open", opened.append)
    settings = Settings(app_name="acme", system_database_url="postgresql://h/sys", app_version="v3")
    runtime.configure(settings)
    runtime.configure(settings, executor_id="mappd-cli-1")
    assert seen[0] == {
        "name": "acme",
        "system_database_url": "postgresql://h/sys",
        "application_version": "v3",
        "enable_otlp": True,  # DBOS spans land on the global provider telemetry.setup installs
    }
    assert seen[1]["executor_id"] == "mappd-cli-1"
    assert opened == [settings, settings], "configure opens the app-DB datasource"


def test_lifespan_applies_schedules_after_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []

    async def tick(_at: datetime, _context: object) -> None:
        return None

    async def apply(inputs: list[dict[str, object]]) -> None:
        events.append([(i["schedule_name"], i["schedule"], i["workflow_fn"]) for i in inputs])

    async def drain(_: Settings) -> bool:
        return True

    class Provider:
        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(runtime, "configure", lambda _: None)
    monkeypatch.setattr(DBOS, "launch", lambda: events.append("launch"))
    monkeypatch.setattr(DBOS, "list_schedules_async", no_schedules)
    monkeypatch.setattr(DBOS, "apply_schedules_async", apply)
    monkeypatch.setattr(runtime, "drain", drain)
    schedules = [Schedule(name="demo.tick", workflow=tick, cron="0 * * * * *")]

    async def serve() -> None:
        lifespan = runtime.lifespan(
            Settings(), telemetry_setup=lambda _: Provider(), schedules=schedules
        )
        async with lifespan(FastAPI()):
            pass

    asyncio.run(serve())
    assert events == ["launch", [("demo.tick", "0 * * * * *", tick)]]


def test_schedule_context_reaches_dbos(monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[dict[str, object]] = []

    async def tick(_at: datetime, _context: object) -> None:
        return None

    async def apply(inputs: list[dict[str, object]]) -> None:
        applied.extend(inputs)

    async def drain(_: Settings) -> bool:
        return True

    class Provider:
        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(runtime, "configure", lambda _: None)
    monkeypatch.setattr(DBOS, "launch", lambda: None)
    monkeypatch.setattr(DBOS, "list_schedules_async", no_schedules)
    monkeypatch.setattr(DBOS, "apply_schedules_async", apply)
    monkeypatch.setattr(runtime, "drain", drain)
    schedules = [
        Schedule(name="support.mail", workflow=tick, cron="*/30 * * * * *", context="support")
    ]

    async def serve() -> None:
        lifespan = runtime.lifespan(
            Settings(), telemetry_setup=lambda _: Provider(), schedules=schedules
        )
        async with lifespan(FastAPI()):
            pass

    asyncio.run(serve())
    assert applied[0]["context"] == "support"


def test_lifespan_deletes_schedules_no_longer_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    # Turning polling off drops its schedule from the list: the stored row must go too.
    deleted: list[str] = []

    async def tick(_at: datetime, _context: object) -> None:
        return None

    async def apply(_inputs: list[dict[str, object]]) -> None:
        return None

    async def listed(**_kw: object) -> list[dict[str, object]]:
        return [{"schedule_name": "demo.tick"}, {"schedule_name": "demo.poll"}]

    async def delete(name: str) -> None:
        deleted.append(name)

    async def drain(_: Settings) -> bool:
        return True

    class Provider:
        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(runtime, "configure", lambda _: None)
    monkeypatch.setattr(DBOS, "launch", lambda: None)
    monkeypatch.setattr(DBOS, "apply_schedules_async", apply)
    monkeypatch.setattr(DBOS, "list_schedules_async", listed)
    monkeypatch.setattr(DBOS, "delete_schedule_async", delete)
    monkeypatch.setattr(runtime, "drain", drain)
    schedules = [Schedule(name="demo.tick", workflow=tick, cron="0 * * * * *")]

    async def serve() -> None:
        lifespan = runtime.lifespan(
            Settings(), telemetry_setup=lambda _: Provider(), schedules=schedules
        )
        async with lifespan(FastAPI()):
            pass

    asyncio.run(serve())
    assert deleted == ["demo.poll"]


def test_lifespan_closes_the_dashboard_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []

    async def drain(_: Settings) -> bool:
        return True

    monkeypatch.setattr(runtime, "configure", lambda _: None)
    monkeypatch.setattr(DBOS, "launch", lambda: None)
    monkeypatch.setattr(DBOS, "list_schedules_async", no_schedules)
    monkeypatch.setattr(runtime, "drain", drain)
    monkeypatch.setattr(workflows, "close_shared", lambda: closed.append("client"))
    monkeypatch.setattr(workflow_stats, "close", lambda: closed.append("stats"))

    class Provider:
        def shutdown(self) -> None:
            closed.append("telemetry")

    async def serve() -> None:
        async with runtime.lifespan(Settings(), telemetry_setup=lambda _: Provider())(FastAPI()):
            pass

    asyncio.run(serve())
    assert closed == ["client", "stats", "telemetry"]


def test_dbos_noise_is_dropped_and_the_rest_kept() -> None:
    quiet = runtime.QuietDbos()

    def record(message: str) -> logging.LogRecord:
        return logging.LogRecord("dbos", logging.INFO, __file__, 1, message, None, None)

    conductor = "To view and manage workflows, connect to DBOS Conductor at: https://x"
    assert not quiet.filter(record(conductor))
    assert not quiet.filter(record("OTLP is enabled but logger provider not set, skipping"))
    assert quiet.filter(record("DBOS launched!"))
