import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import sqlalchemy as sa
import stranded
from dbos import DBOS, SetWorkflowID

from mappd import workflow_stats, workflows
from mappd.db.migrate import sqlalchemy_url

if TYPE_CHECKING:
    import pytest

    from mappd.settings import Settings


def _since() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=5)


def test_counts_by_status_and_name(dbos_runtime: Settings) -> None:
    before = workflow_stats.stats(dbos_runtime, since=_since(), stranded=0)
    with SetWorkflowID(f"done-{uuid4()}"):
        DBOS.start_workflow(stranded.flaky_workflow).get_result()
    stranded.fail()
    after = workflow_stats.stats(dbos_runtime, since=_since(), stranded=3)
    assert after.by_status["SUCCESS"] >= before.by_status.get("SUCCESS", 0) + 1
    assert after.by_status["ERROR"] >= before.by_status.get("ERROR", 0) + 1
    assert after.by_name["test.flaky"] >= before.by_name.get("test.flaky", 0) + 2
    assert after.stranded == 3, "the caller's stranded count is passed through"
    assert "test" in after.processes
    later = workflow_stats.stats(
        dbos_runtime, since=datetime.now(UTC) + timedelta(hours=1), stranded=0
    )
    assert later.by_name.get("test.flaky", 0) == 0, "the window excludes older workflows"


def test_other_applications_are_not_counted(dbos_runtime: Settings) -> None:
    other = replace(dbos_runtime, app_name="another-app")
    mine_before = workflow_stats.stats(dbos_runtime, since=_since(), stranded=0)
    theirs_before = workflow_stats.stats(other, since=_since(), stranded=0)
    wid = stranded.strand(dbos_runtime, "stats-other", app_name="another-app")
    try:
        mine = workflow_stats.stats(dbos_runtime, since=_since(), stranded=0)
        theirs = workflow_stats.stats(other, since=_since(), stranded=0)
        assert mine.by_name.get("test.stranded", 0) == mine_before.by_name.get("test.stranded", 0)
        assert theirs.by_name["test.stranded"] == theirs_before.by_name.get("test.stranded", 0) + 1
    finally:
        workflows.cancel(other, wid)


def test_queue_depth_counts_waiting_and_running(dbos_runtime: Settings) -> None:
    ids = [f"queued-{uuid4()}" for _ in range(2)]
    for wid in ids:
        with SetWorkflowID(wid):
            stranded.dashboard_queue().enqueue(stranded.waiting_workflow)
    try:
        deadline = time.monotonic() + 30
        depth = None
        while time.monotonic() < deadline:
            queues = workflow_stats.stats(dbos_runtime, since=_since(), stranded=0).queues
            depth = next((q for q in queues if q.name == "test-dashboard"), None)
            if depth is not None and (depth.waiting, depth.running) == (1, 1):
                break
            time.sleep(0.2)
        assert depth is not None
        assert (depth.waiting, depth.running) == (1, 1)
        assert all(not q.name.startswith("_dbos") for q in queues)
    finally:
        for wid in ids:
            DBOS.send(wid, "stop", topic="go")
        for wid in ids:
            DBOS.retrieve_workflow(wid).get_result()


def test_names_lists_this_applications_workflows(dbos_runtime: Settings) -> None:
    with SetWorkflowID(f"done-{uuid4()}"):
        DBOS.start_workflow(stranded.flaky_workflow).get_result()
    names = workflow_stats.names(dbos_runtime)
    assert "test.flaky" in names
    assert names == sorted(names)


def test_the_table_still_has_the_columns_we_read(dbos_runtime: Settings) -> None:
    engine = sa.create_engine(sqlalchemy_url(dbos_runtime.system_database_url))
    try:
        with engine.connect() as conn:
            conn.execute(sa.select(*workflow_stats.STATUS.c).limit(1)).all()
    finally:
        engine.dispose()


def test_the_engine_is_created_once_under_concurrency(
    dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_stats.close()
    made: list[str] = []
    real = sa.create_engine

    def slow(url: str, **kwargs: object) -> sa.Engine:
        time.sleep(0.2)
        made.append(url)
        return real(url, **kwargs)

    monkeypatch.setattr(sa, "create_engine", slow)
    try:
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: workflow_stats.names(dbos_runtime), range(4)))
        assert len(made) == 1
    finally:
        workflow_stats.close()
