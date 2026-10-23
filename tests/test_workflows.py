import contextlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import dbos_async
import pytest
import stranded
from dbos import DBOS, DBOSClient, SetWorkflowID
from dbos._error import DBOSAwaitedWorkflowCancelledError

from mappd import workflow_stats, workflows
from mappd.cli._common import DB_ERRORS
from mappd.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from dbos._enqueue_options import EnqueueOptions


def status_of(settings: Settings, workflow_id: str, wanted: str, timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        [info] = workflows.list_workflows(settings, status=None, limit=1000, ids=[workflow_id])
        status = info.status
        if status == wanted:
            return status
        time.sleep(0.2)
    return status


def test_stranded_lists_pending_workflows_of_other_versions(dbos_runtime: Settings) -> None:
    wid = stranded.strand(dbos_runtime, "old")
    try:
        found = {w.workflow_id: w for w in workflows.list_workflows(dbos_runtime, stranded=True)}
        assert found[wid].app_version == "old"
        assert found[wid].status == "PENDING"
        assert found[wid].name == "test.stranded"
    finally:
        workflows.cancel(dbos_runtime, wid)


def test_recover_forks_onto_the_target_version_and_cancels_the_original(
    dbos_runtime: Settings,
) -> None:
    wid = stranded.strand(dbos_runtime, "old")
    [moved] = [m for m in workflows.recover(dbos_runtime, "test") if m.workflow_id == wid]
    assert moved.old_version == "old"
    assert moved.start_step == 2, "resume after the last recorded step"
    assert status_of(dbos_runtime, moved.new_workflow_id, "SUCCESS") == "SUCCESS"
    assert status_of(dbos_runtime, wid, "CANCELLED") == "CANCELLED"
    with workflows.client(dbos_runtime) as c:
        steps = [s["function_name"] for s in c.list_workflow_steps(moved.new_workflow_id)]
    assert steps == ["test.stranded.first", "test.stranded.second"]


def test_other_applications_are_neither_listed_nor_recovered(dbos_runtime: Settings) -> None:
    # DBOS requires version names to be unique across applications.
    wid = stranded.strand(dbos_runtime, "another-old", app_name="another-app")
    try:
        assert wid not in {
            w.workflow_id for w in workflows.list_workflows(dbos_runtime, stranded=True)
        }
        assert wid not in {m.workflow_id for m in workflows.recover(dbos_runtime, "test")}
    finally:
        workflows.cancel(replace(dbos_runtime, app_name="another-app"), wid)


def test_cancel_marks_a_running_workflow_cancelled(dbos_runtime: Settings) -> None:
    wid = f"waiting-{uuid4()}"
    with SetWorkflowID(wid):
        handle = DBOS.start_workflow(stranded.waiting_workflow)
    workflows.cancel(dbos_runtime, wid)
    assert status_of(dbos_runtime, wid, "CANCELLED") == "CANCELLED"
    # Wake the blocked recv so the workflow's thread ends inside this test.
    DBOS.send(wid, "stop", topic="go")
    with pytest.raises(DBOSAwaitedWorkflowCancelledError):
        handle.get_result()


def test_cancel_unknown_workflow_raises(dbos_runtime: Settings) -> None:
    with pytest.raises(workflows.WorkflowNotFoundError):
        workflows.cancel(dbos_runtime, f"nope-{uuid4()}")


def test_retry_forks_an_errored_workflow_at_the_failed_step(dbos_runtime: Settings) -> None:
    wid = stranded.fail()
    moved = workflows.retry(dbos_runtime, wid)
    assert (moved.workflow_id, moved.start_step) == (wid, 2), "retry at the failed step"
    assert status_of(dbos_runtime, moved.new_workflow_id, "SUCCESS") == "SUCCESS"
    [again] = workflows.list_workflows(dbos_runtime, ids=[wid])
    assert again.status == "ERROR", "the original stays on record"


def test_retry_returns_the_existing_fork(dbos_runtime: Settings) -> None:
    wid = stranded.fail()
    first = workflows.retry(dbos_runtime, wid)
    assert status_of(dbos_runtime, first.new_workflow_id, "SUCCESS") == "SUCCESS"
    assert workflows.retry(dbos_runtime, wid).new_workflow_id == first.new_workflow_id


def test_retry_refuses_a_workflow_not_in_error(dbos_runtime: Settings) -> None:
    wid = f"done-{uuid4()}"
    with SetWorkflowID(wid):
        DBOS.start_workflow(stranded.flaky_workflow).get_result()
    with pytest.raises(workflows.WorkflowNotFailedError, match="SUCCESS"):
        workflows.retry(dbos_runtime, wid)
    with pytest.raises(workflows.WorkflowNotFoundError):
        workflows.retry(dbos_runtime, f"nope-{uuid4()}")


def test_retry_refuses_an_unregistered_target(dbos_runtime: Settings) -> None:
    with pytest.raises(workflows.UnknownVersionError, match="typo"):
        workflows.retry(dbos_runtime, stranded.fail(), to_version="typo")


def test_recover_refuses_an_unregistered_target(dbos_runtime: Settings) -> None:
    wid = stranded.strand(dbos_runtime, "old")
    try:
        with pytest.raises(workflows.UnknownVersionError, match="typo"):
            workflows.recover(dbos_runtime, "typo")
        [info] = workflows.list_workflows(dbos_runtime, ids=[wid])
        assert info.status == "PENDING", "nothing is cancelled when the target is wrong"
    finally:
        workflows.cancel(dbos_runtime, wid)


def test_enqueued_workflow_of_another_version_is_stranded_and_recovered(
    dbos_runtime: Settings,
) -> None:
    done = f"done-{uuid4()}"
    with SetWorkflowID(done):
        assert DBOS.start_workflow(stranded.stranded_workflow).get_result() == "two"
    with workflows.client(dbos_runtime) as c:
        queued = c.fork_workflow(done, 1, application_version="old").workflow_id
    assert queued in {w.workflow_id for w in workflows.list_workflows(dbos_runtime, stranded=True)}
    [moved] = [m for m in workflows.recover(dbos_runtime, "test") if m.workflow_id == queued]
    assert status_of(dbos_runtime, moved.new_workflow_id, "SUCCESS") == "SUCCESS"


def test_recover_after_a_crash_between_fork_and_cancel_does_not_fork_again(
    dbos_runtime: Settings,
) -> None:
    wid = stranded.strand(dbos_runtime, "old")
    with workflows.client(dbos_runtime) as c:
        first_fork = c.fork_workflow(wid, 2, application_version="test").workflow_id
    [moved] = [m for m in workflows.recover(dbos_runtime, "test") if m.workflow_id == wid]
    assert moved.new_workflow_id == first_fork
    with workflows.client(dbos_runtime) as c:
        forks = c.list_workflows(forked_from=wid, load_input=False, load_output=False)
    assert [f.workflow_id for f in forks] == [first_fork]
    assert status_of(dbos_runtime, wid, "CANCELLED") == "CANCELLED"


def test_abandoned_cli_run_is_stranded_and_recovered(dbos_runtime: Settings) -> None:
    executor = f"{workflows.CLI_EXECUTOR_PREFIX}gone"
    wid = stranded.strand(dbos_runtime, "test", executor_id=executor)
    recent = {w.workflow_id for w in workflows.list_workflows(dbos_runtime, stranded=True)}
    assert wid not in recent, "a CLI run younger than the threshold may still be running"
    aged = workflows.list_workflows(dbos_runtime, stranded=True, abandoned_after=0)
    assert wid in {w.workflow_id for w in aged}
    [moved] = [
        m
        for m in workflows.recover(dbos_runtime, "test", abandoned_after=0)
        if m.workflow_id == wid
    ]
    assert status_of(dbos_runtime, moved.new_workflow_id, "SUCCESS") == "SUCCESS"


@pytest.mark.timeout(20)
def test_dead_system_database_fails_fast() -> None:
    dead = Settings(system_database_url="postgresql://mappd:mappd@127.0.0.1:9/nope")
    with pytest.raises(DB_ERRORS):
        workflows.list_workflows(dead)


def done(workflow: Callable[[], str] | None = None) -> str:
    """A finished `test.flaky` run (two steps, SUCCESS)."""
    wid = f"done-{uuid4()}"
    with SetWorkflowID(wid):
        DBOS.start_workflow(workflow or stranded.flaky_workflow).get_result()
    return wid


def sent(auto: bool = False) -> tuple[str, str]:
    """A finished `test.sender` run; returns its workflow ID and run ID."""
    run_id, wid = f"run-{uuid4()}", f"sender-{uuid4()}"
    with SetWorkflowID(wid):
        assert dbos_async.run(stranded.sender_workflow(run_id, auto)) == "last"
    return wid, run_id


@pytest.fixture
def outbox(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    return dbos_runtime


def test_list_filters_by_name_version_time_and_prefix(dbos_runtime: Settings) -> None:
    prefix = f"pref-{uuid4()}"
    wid = f"{prefix}-a"
    with SetWorkflowID(wid):
        DBOS.start_workflow(stranded.flaky_workflow).get_result()

    def ids(**filters: object) -> set[str]:
        rows = workflows.list_workflows(dbos_runtime, limit=500, **filters)  # type: ignore[arg-type]
        return {w.workflow_id for w in rows}

    assert ids(id_prefix=prefix) == {wid}
    assert wid in ids(name="test.flaky", id_prefix=prefix)
    assert wid not in ids(name="test.stranded", id_prefix=prefix)
    assert wid in ids(version="test", id_prefix=prefix)
    assert wid not in ids(version="nope", id_prefix=prefix)
    assert wid not in ids(since=datetime.now(UTC) + timedelta(hours=1), id_prefix=prefix)
    assert wid not in ids(until=datetime.now(UTC) - timedelta(hours=1), id_prefix=prefix)
    [row] = workflows.list_workflows(dbos_runtime, id_prefix=prefix)
    assert row.process == "test"
    assert row.status == "SUCCESS"
    assert row.duration_ms is not None
    assert row.created_at is not None
    assert row.created_at.tzinfo is not None


def test_list_filters_by_process(dbos_runtime: Settings) -> None:
    wid = done()
    rows = workflows.list_workflows(dbos_runtime, process="test", limit=500)
    assert wid in {w.workflow_id for w in rows}
    assert all(w.name.startswith("test.") for w in rows)
    assert workflows.list_workflows(dbos_runtime, process="no-such-process") == []


def test_scheduled_ticks_can_be_left_out(dbos_runtime: Settings) -> None:
    tick = f"sched-test.tick-{uuid4()}"  # how DBOS names a scheduled run
    with SetWorkflowID(tick):
        DBOS.start_workflow(stranded.stranded_workflow).get_result()
    wid = done()
    every = {w.workflow_id for w in workflows.list_workflows(dbos_runtime, limit=500)}
    assert {tick, wid} <= every
    rows = workflows.list_workflows(dbos_runtime, scheduled=False, limit=500)
    assert wid in {w.workflow_id for w in rows}
    assert "test.stranded" not in {w.name for w in rows}, "a scheduled workflow's runs all go"


def test_stats_follow_the_process_filter(dbos_runtime: Settings) -> None:
    done()
    counted = workflows.stats(dbos_runtime, window="1h", process="test")
    assert counted.by_name
    assert all(n.startswith("test.") for n in counted.by_name)
    nothing = workflows.stats(dbos_runtime, window="1h", process="no-such-process")
    assert nothing.by_status == {}


def test_list_pages_with_offset(dbos_runtime: Settings) -> None:
    done()
    done()
    first = workflows.list_workflows(dbos_runtime, name="test.flaky", limit=1)
    second = workflows.list_workflows(dbos_runtime, name="test.flaky", limit=1, offset=1)
    assert len(first) == len(second) == 1
    assert first[0].workflow_id != second[0].workflow_id


def test_stranded_list_pages_after_filtering(dbos_runtime: Settings) -> None:
    ids = [stranded.strand(dbos_runtime, "old") for _ in range(2)]
    try:
        everything = workflows.list_workflows(dbos_runtime, stranded=True)
        page = workflows.list_workflows(dbos_runtime, stranded=True, limit=1, offset=1)
        assert [w.workflow_id for w in page] == [w.workflow_id for w in everything[1:2]]
        assert all(w.stranded for w in everything)
    finally:
        for wid in ids:
            workflows.cancel(dbos_runtime, wid)


def test_get_returns_steps_links_and_kinds(outbox: Settings) -> None:
    wid, run_id = sent()
    detail = workflows.get(outbox, wid)
    assert [s.name for s in detail.steps] == [
        "test.sender.first",
        "test.sender.send",
        "test.sender.last",
    ]
    assert [s.kind for s in detail.steps] == ["step", "action", "step"]
    [ref] = detail.actions
    assert ref.idempotency_key == f"test.sender:{run_id}"
    assert ref.status == "done"
    assert not ref.per_workflow
    assert detail.input is not None
    assert run_id in detail.input.text
    assert detail.output is not None
    assert detail.output.text == "last"
    assert detail.root_workflow_id == wid
    assert detail.allowed == ["fork"]
    assert detail.payloads


def test_get_hides_payloads_when_off(outbox: Settings) -> None:
    wid, _ = sent()
    detail = workflows.get(replace(outbox, workflows_payloads=False), wid)
    assert detail.input is None
    assert detail.output is None
    assert all(s.output is None for s in detail.steps)
    assert not detail.payloads
    failed = workflows.get(outbox, stranded.fail(), payloads=False)
    assert failed.error is not None
    assert "downstream is down" in failed.error, "errors stay with payloads off"


def test_get_follows_parents_to_the_root(dbos_runtime: Settings) -> None:
    parent = done(stranded.parent_workflow)
    [child] = workflows.get(dbos_runtime, parent).children
    detail = workflows.get(dbos_runtime, child)
    assert detail.parent_id == parent
    assert detail.root_workflow_id == parent
    assert workflows.get(dbos_runtime, parent).steps[0].kind == "child"


def test_get_unknown_raises(dbos_runtime: Settings) -> None:
    with pytest.raises(workflows.WorkflowNotFoundError):
        workflows.get(dbos_runtime, f"nope-{uuid4()}")


def test_resume_continues_a_cancelled_workflow(dbos_runtime: Settings) -> None:
    wid = f"parked-{uuid4()}"
    with workflows.client(dbos_runtime) as c:
        options: EnqueueOptions = {
            "workflow_name": "test.flaky",
            "queue_name": "test-unserved",
            "workflow_id": wid,
            "app_version": "test",
        }
        c.enqueue(options)
    workflows.cancel(dbos_runtime, wid)
    assert status_of(dbos_runtime, wid, "CANCELLED") == "CANCELLED"
    assert workflows.get(dbos_runtime, wid).allowed == ["resume", "fork"]
    moved = workflows.resume(dbos_runtime, wid)
    assert moved.new_workflow_id == wid
    assert status_of(dbos_runtime, wid, "SUCCESS") == "SUCCESS"


def test_resume_refuses_another_version(dbos_runtime: Settings) -> None:
    wid = stranded.strand(dbos_runtime, "old")
    workflows.cancel(dbos_runtime, wid)
    assert workflows.get(dbos_runtime, wid).allowed == ["fork"]
    with pytest.raises(workflows.WorkflowStateError, match="old"):
        workflows.resume(dbos_runtime, wid)


def test_resume_refuses_a_running_workflow(dbos_runtime: Settings) -> None:
    wid = stranded.strand(dbos_runtime, "old")
    try:
        with pytest.raises(workflows.WorkflowStateError, match="PENDING"):
            workflows.resume(dbos_runtime, wid)
    finally:
        workflows.cancel(dbos_runtime, wid)


def test_fork_replays_before_the_step_and_reruns_after(dbos_runtime: Settings) -> None:
    wid = done()
    moved = workflows.fork(dbos_runtime, wid, 2)
    assert (moved.workflow_id, moved.start_step) == (wid, 2)
    assert status_of(dbos_runtime, moved.new_workflow_id, "SUCCESS") == "SUCCESS"
    original = workflows.get(dbos_runtime, wid).steps
    fork = workflows.get(dbos_runtime, moved.new_workflow_id)
    assert fork.forked_from == wid
    assert fork.steps[0].started_at == original[0].started_at, "step 1 is copied, not re-run"
    assert fork.steps[1].started_at != original[1].started_at, "step 2 runs again"
    assert moved.new_workflow_id in workflows.get(dbos_runtime, wid).forks


def test_fork_refuses_a_running_workflow(dbos_runtime: Settings) -> None:
    wid = stranded.strand(dbos_runtime, "old")
    try:
        with pytest.raises(workflows.WorkflowStateError, match="PENDING"):
            workflows.fork(dbos_runtime, wid, 1)
    finally:
        workflows.cancel(dbos_runtime, wid)


def test_fork_refuses_while_an_earlier_fork_runs(dbos_runtime: Settings) -> None:
    wid = f"waited-{uuid4()}"
    with SetWorkflowID(wid):
        handle = DBOS.start_workflow(stranded.waiting_workflow)
    DBOS.send(wid, "go", topic="go")
    assert handle.get_result() == "go"
    first = workflows.fork(dbos_runtime, wid, 1)
    try:
        assert status_of(dbos_runtime, first.new_workflow_id, "PENDING") == "PENDING"
        with pytest.raises(workflows.WorkflowStateError, match=first.new_workflow_id):
            workflows.fork(dbos_runtime, wid, 1)
    finally:
        DBOS.send(first.new_workflow_id, "stop", topic="go")
    assert status_of(dbos_runtime, first.new_workflow_id, "SUCCESS") == "SUCCESS"


@pytest.mark.parametrize("step", [0, 4])
def test_fork_refuses_a_step_out_of_range(dbos_runtime: Settings, step: int) -> None:
    with pytest.raises(workflows.StepOutOfRangeError):
        workflows.fork(dbos_runtime, done(), step)


def test_a_fork_does_not_resend_a_business_keyed_action(outbox: Settings) -> None:
    wid, run_id = sent()
    assert stranded.SENT.count(run_id) == 1
    moved = workflows.fork(outbox, wid, 2)
    assert status_of(outbox, moved.new_workflow_id, "SUCCESS") == "SUCCESS"
    assert stranded.SENT.count(run_id) == 1, "the outbox row is done: nothing is sent again"


def test_a_fork_resends_an_auto_keyed_action(outbox: Settings) -> None:
    wid, run_id = sent(auto=True)
    [ref] = workflows.get(outbox, wid).actions
    assert ref.per_workflow
    moved = workflows.fork(outbox, wid, 2)
    assert status_of(outbox, moved.new_workflow_id, "SUCCESS") == "SUCCESS"
    assert stranded.SENT.count(run_id) == 2, "an AUTO key includes the workflow ID"


def test_cancel_refuses_a_finished_workflow(dbos_runtime: Settings) -> None:
    with pytest.raises(workflows.WorkflowStateError, match="SUCCESS"):
        workflows.cancel(dbos_runtime, done())


def test_recover_only_the_given_ids(dbos_runtime: Settings) -> None:
    mine, other = (stranded.strand(dbos_runtime, "old") for _ in range(2))
    try:
        moved = workflows.recover(dbos_runtime, "test", ids=[mine])
        assert [m.workflow_id for m in moved] == [mine]
        [left] = workflows.list_workflows(dbos_runtime, ids=[other])
        assert left.status == "PENDING"
    finally:
        workflows.cancel(dbos_runtime, other)


def test_stats_counts_the_stranded(dbos_runtime: Settings) -> None:
    wid = stranded.strand(dbos_runtime, "old")
    try:
        found = workflows.stats(dbos_runtime, window="1h")
        assert found.stranded >= 1
        assert found.by_status["PENDING"] >= 1
    finally:
        workflows.cancel(dbos_runtime, wid)


def test_shared_client_is_reused_until_closed(dbos_runtime: Settings) -> None:
    with (
        workflows.client(dbos_runtime, shared=True) as a,
        workflows.client(dbos_runtime, shared=True) as b,
    ):
        assert a is b
    workflows.close_shared()
    with workflows.client(dbos_runtime, shared=True) as c:
        assert c is not a
    workflows.close_shared()


def test_list_shows_the_error_summary_of_failed_rows(dbos_runtime: Settings) -> None:
    wid = stranded.fail()
    [row] = workflows.list_workflows(dbos_runtime, ids=[wid])
    assert row.error_summary == "RuntimeError: downstream is down"


def test_shared_client_is_created_once_under_concurrency(
    dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[object] = []
    real = workflows._new_client

    def slow(settings: Settings) -> object:
        time.sleep(0.2)  # widen the window two threads would both create in
        made.append(object())
        return real(settings)

    monkeypatch.setattr(workflows, "_new_client", slow)

    def use() -> int:
        with workflows.client(dbos_runtime, shared=True) as c:
            return id(c)

    try:
        with ThreadPoolExecutor(4) as pool:
            ids = set(pool.map(lambda _: use(), range(4)))
        assert len(ids) == 1
        assert len(made) == 1
    finally:
        workflows.close_shared()


def test_each_action_step_gets_its_own_outbox_row(outbox: Settings) -> None:
    run_id, wid = f"run-{uuid4()}", f"twice-{uuid4()}"
    with SetWorkflowID(wid):
        dbos_async.run(stranded.sender_twice(run_id))
    first, second = workflows.get(outbox, wid).steps
    assert first.outbox is not None
    assert second.outbox is not None
    assert first.outbox.idempotency_key.endswith(":a")
    assert second.outbox.idempotency_key.endswith(":b")


def test_retry_ignores_a_failed_manual_fork(dbos_runtime: Settings) -> None:
    wid = stranded.fail()
    os.environ[stranded.STALL] = "1"
    try:
        failed_fork = workflows.fork(dbos_runtime, wid, 1).new_workflow_id
        assert status_of(dbos_runtime, failed_fork, "ERROR") == "ERROR"
    finally:
        del os.environ[stranded.STALL]
    retried = workflows.retry(dbos_runtime, wid)
    assert retried.new_workflow_id != failed_fork, "a failed fork is not the retry"
    assert status_of(dbos_runtime, retried.new_workflow_id, "SUCCESS") == "SUCCESS"


def test_actions_reuse_the_shared_client(
    dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid, failed = done(), stranded.fail()
    workflows.close_shared()
    made: list[int] = []
    real = workflows._new_client

    def counting(settings: Settings) -> DBOSClient:
        made.append(1)
        return real(settings)

    monkeypatch.setattr(workflows, "_new_client", counting)
    try:
        workflows.registered_versions(dbos_runtime, shared=True)
        workflows.fork(dbos_runtime, wid, 2, shared=True)
        workflows.retry(dbos_runtime, failed, shared=True)
        with contextlib.suppress(workflows.WorkflowStateError):
            workflows.cancel(dbos_runtime, wid, shared=True)
        with contextlib.suppress(workflows.WorkflowStateError):
            workflows.resume(dbos_runtime, wid, shared=True)
        workflows.recover(dbos_runtime, "test", ids=[wid], shared=True)
        assert len(made) == 1
    finally:
        workflows.close_shared()


@pytest.mark.parametrize("abandoned_after", [0.0, workflows.CLI_ABANDONED_AFTER])
def test_the_stranded_count_matches_the_stranded_list(
    dbos_runtime: Settings, abandoned_after: float
) -> None:
    old = stranded.strand(dbos_runtime, "old")
    cli = stranded.strand(dbos_runtime, "test", executor_id=f"{workflows.CLI_EXECUTOR_PREFIX}gone")
    try:
        listed = workflows.list_workflows(
            dbos_runtime, stranded=True, limit=10_000, abandoned_after=abandoned_after
        )
        counted = workflow_stats.stranded(
            dbos_runtime, current=dbos_runtime.app_version, abandoned_after=abandoned_after
        )
        assert counted == len(listed)
    finally:
        for wid in (old, cli):
            with contextlib.suppress(workflows.WorkflowStateError):
                workflows.cancel(dbos_runtime, wid)


def test_stats_does_not_list_workflows(
    dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("stats must count in SQL, not list rows")

    monkeypatch.setattr(workflows, "list_workflows", refuse)
    assert workflows.stats(dbos_runtime, window="1h").stranded >= 0
