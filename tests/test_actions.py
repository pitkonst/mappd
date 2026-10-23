import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import dbos_async
import pytest
import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID
from dbos._registrations import get_dbos_func_name

from mappd.actions import (
    AUTO,
    ActionInProgressError,
    OutcomeUnknownError,
    PermanentError,
    action,
    list_actions,
    per,
    perform,
    resolve,
    retryable,
)
from mappd.db import Connection, connect, run
from mappd.db.tables import ACTIONS

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mappd.settings import Settings

SENT: list[str] = []


@action("test.notify")
def notify(idempotency_key: str, to: str) -> dict[str, Any]:
    SENT.append(to)
    return {"sent_to": to, "key": idempotency_key}


@action("test.keyless", on_unknown="escalate")
def keyless(idempotency_key: str) -> dict[str, Any]:
    return {"ok": True}


@DBOS.workflow()
async def keyless_once(key: str) -> dict[str, Any]:
    return await keyless(key)


@DBOS.workflow()
async def notify_twice(key: str) -> list[dict[str, Any]]:
    return [await notify(key, to="anna"), await notify(key, to="anna")]


@DBOS.workflow()
async def notify_auto_twice() -> list[dict[str, Any]]:
    return [await notify(AUTO, to="anna"), await notify(AUTO, to="anna")]


@DBOS.workflow()
async def notify_per(entity: str) -> dict[str, Any]:
    return await notify(per(entity), to="anna")


@action("test.by_arg", per="to")
def notify_arg(idempotency_key: str, to: str) -> dict[str, Any]:
    SENT.append(to)
    return {"sent_to": to, "key": idempotency_key}


@DBOS.workflow()
async def notify_arg_once(to: str) -> dict[str, Any]:
    return await notify_arg(to)


def test_bare_action_derives_its_name_from_the_module() -> None:
    def send_thing(idempotency_key: str) -> dict[str, Any]:
        return {"key": idempotency_key}

    send_thing.__module__ = "processes.demo.tools"
    assert get_dbos_func_name(action(send_thing)) == "demo.send_thing"


class Receiver:
    """Counts calls; answers like a mailer."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        return {"message_id": f"m-{self.calls}"}


def key() -> str:
    return f"test-{uuid4()}"


def row(conn: Connection, k: str) -> dict[str, Any]:
    found = conn.execute(
        "SELECT status, attempts, result, last_error, claimed_by FROM actions"
        " WHERE idempotency_key = %s",
        (k,),
    ).fetchone()
    assert found is not None
    return dict(found)


def insert(conn: Connection, k: str, kind: str = "test.send") -> None:
    conn.execute(
        "INSERT INTO actions (kind, idempotency_key, payload) VALUES (%s, %s, '{}')", (kind, k)
    )


def set_status(
    conn: Connection, k: str, status: str, claimed_by: str | None = None, age: float = 0.0
) -> None:
    conn.execute(
        "UPDATE actions SET status = %s, claimed_by = %s,"
        " claimed_at = now() - make_interval(secs => %s) WHERE idempotency_key = %s",
        (status, claimed_by, age, k),
    )


@pytest.fixture
def conn(migrated: Settings) -> Iterator[Connection]:
    with connect(migrated) as c:
        yield c


def test_first_call_acts_and_records_done(conn: Connection) -> None:
    send, k = Receiver(), key()
    assert perform(conn, "test.send", k, {"to": "anna"}, send) == {"message_id": "m-1"}
    assert row(conn, k) == {
        "status": "done",
        "attempts": 1,
        "result": {"message_id": "m-1"},
        "last_error": None,
        "claimed_by": None,
    }


def test_done_action_returns_the_stored_result_without_acting(conn: Connection) -> None:
    send, k = Receiver(), key()
    perform(conn, "test.send", k, {}, send)
    assert perform(conn, "test.send", k, {}, send) == {"message_id": "m-1"}
    assert send.calls == 1
    assert row(conn, k)["attempts"] == 1


def test_the_claim_is_committed_before_acting(migrated: Settings, conn: Connection) -> None:
    k = key()
    seen: list[str] = []

    def send() -> dict[str, Any]:
        with connect(migrated) as other:
            seen.append(row(other, k)["status"])
        return {"ok": True}

    perform(conn, "test.send", k, {}, send)
    assert seen == ["sending"], "another connection sees the claim: no transaction is open"


def test_a_failed_action_is_recorded_and_acts_again(conn: Connection) -> None:
    k = key()
    calls: list[int] = []

    def flaky() -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("receiver down")
        return {"ok": True}

    with pytest.raises(ConnectionError):
        perform(conn, "test.flaky", k, {}, flaky)
    assert row(conn, k)["status"] == "failed"
    assert row(conn, k)["last_error"] == "ConnectionError: receiver down"
    assert perform(conn, "test.flaky", k, {}, flaky) == {"ok": True}
    assert row(conn, k)["attempts"] == 2


def test_resend_policy_acts_again_after_its_own_interrupted_attempt(conn: Connection) -> None:
    send, k = Receiver(), key()
    insert(conn, k)
    set_status(conn, k, "sending", claimed_by="wf-1")
    assert perform(conn, "test.send", k, {}, send, claimed_by="wf-1") == {"message_id": "m-1"}
    assert row(conn, k)["status"] == "done"


def test_escalate_policy_stops_after_its_own_interrupted_attempt(conn: Connection) -> None:
    send, k = Receiver(), key()
    insert(conn, k)
    set_status(conn, k, "sending", claimed_by="wf-1")
    with pytest.raises(OutcomeUnknownError):
        perform(conn, "test.send", k, {}, send, on_unknown="escalate", claimed_by="wf-1")
    assert send.calls == 0
    assert row(conn, k)["status"] == "unknown"


def test_a_live_attempt_by_another_workflow_is_left_alone(conn: Connection) -> None:
    send, k = Receiver(), key()
    insert(conn, k)
    set_status(conn, k, "sending", claimed_by="wf-2")
    with pytest.raises(ActionInProgressError, match="wf-2"):
        perform(conn, "test.send", k, {}, send, claimed_by="wf-1")
    assert send.calls == 0
    assert row(conn, k)["status"] == "sending"


def test_a_stale_attempt_by_another_workflow_follows_the_policy(conn: Connection) -> None:
    send, k = Receiver(), key()
    insert(conn, k)
    set_status(conn, k, "sending", claimed_by="wf-2", age=600)
    assert perform(conn, "test.send", k, {}, send, claimed_by="wf-1", lease_seconds=300) == {
        "message_id": "m-1"
    }


def test_a_function_reporting_an_unknown_outcome_stops_the_action(conn: Connection) -> None:
    k = key()

    def timeout() -> dict[str, Any]:
        raise OutcomeUnknownError("timed out after sending")

    with pytest.raises(OutcomeUnknownError):
        perform(conn, "test.send", k, {}, timeout)
    assert row(conn, k)["status"] == "unknown"
    assert row(conn, k)["last_error"] == "timed out after sending"
    send = Receiver()
    with pytest.raises(OutcomeUnknownError):
        perform(conn, "test.send", k, {}, send)
    assert send.calls == 0


@pytest.mark.parametrize(("took_effect", "calls"), [(True, 0), (False, 1)])
def test_resolve_settles_an_unknown_action(conn: Connection, took_effect: bool, calls: int) -> None:
    k = key()
    insert(conn, k)
    set_status(conn, k, "unknown")
    resolve(conn, k, took_effect=took_effect, operator="olga", result={"message_id": "checked"})
    send = Receiver()
    out = perform(conn, "test.send", k, {}, send)
    assert send.calls == calls
    assert out == ({"message_id": "checked"} if took_effect else {"message_id": "m-1"})


def test_resolve_needs_an_unknown_action(conn: Connection) -> None:
    with pytest.raises(LookupError):
        resolve(conn, key(), took_effect=True, operator="olga")


def test_concurrent_calls_with_one_key_act_once(migrated: Settings) -> None:
    calls: list[int] = []
    lock = threading.Lock()
    k = key()

    def slow_send() -> dict[str, Any]:
        with lock:
            calls.append(1)
        time.sleep(0.3)
        return {"ok": True}

    def call(_: int) -> dict[str, Any] | None:
        with connect(migrated) as c:
            try:
                return perform(c, "test.slow", k, {}, slow_send)
            except ActionInProgressError:
                return None

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(call, range(4)))
    assert calls == [1]
    assert {"ok": True} in results
    assert all(r in ({"ok": True}, None) for r in results)
    with connect(migrated) as c:
        assert perform(c, "test.slow", k, {}, slow_send) == {"ok": True}


def test_key_reused_by_another_kind_is_an_error(conn: Connection) -> None:
    k = key()
    perform(conn, "test.one", k, {}, lambda: {"ok": True})
    with pytest.raises(ValueError, match=r"already used by test\.one"):
        perform(conn, "test.two", k, {}, lambda: {"ok": True})


def test_key_reused_with_a_different_payload_is_an_error(conn: Connection) -> None:
    k = key()
    perform(conn, "test.send", k, {"to": "anna", "body": "first"}, lambda: {"ok": True})
    with pytest.raises(ValueError, match="different payload"):
        perform(conn, "test.send", k, {"to": "anna", "body": "second"}, lambda: {"ok": True})


def test_first_run_and_replay_return_the_same_types(conn: Connection) -> None:
    when = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    k = key()
    first = perform(conn, "test.stamp", k, {}, lambda: {"at": when})
    replay = perform(conn, "test.stamp", k, {}, lambda: {"at": when})
    assert first == replay == {"at": "2026-10-12 09:00:00+00:00"}


def test_result_must_be_a_json_object(conn: Connection) -> None:
    def bad() -> Any:
        return ["not", "a", "dict"]

    k = key()
    with pytest.raises(TypeError, match="JSON object"):
        perform(conn, "test.bad", k, {}, bad)
    assert row(conn, k)["status"] == "failed"


def test_first_parameter_must_be_the_key() -> None:
    with pytest.raises(TypeError, match="idempotency_key"):

        @action("test.wrong")
        def wrong(to: str, idempotency_key: str) -> dict[str, Any]:  # pragma: no cover
            return {}


def test_empty_key_is_rejected_before_any_side_effect() -> None:
    with pytest.raises(ValueError, match="idempotency key"):
        dbos_async.run(notify("", to="anna"))


def test_auto_key_needs_a_step_inside_a_workflow() -> None:
    with pytest.raises(ValueError, match="explicit idempotency_key"):
        dbos_async.run(notify(AUTO, to="anna"))


def test_auto_key_dedupes_per_step(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each step gets its own derived key, so both sends act; a replay acts on neither."""
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    SENT.clear()
    wid = f"auto-{uuid4()}"
    with SetWorkflowID(wid):
        assert dbos_async.run(notify_auto_twice()) == [
            {"sent_to": "anna", "key": f"{wid}:test.notify:{step}"} for step in (1, 2)
        ]
    assert SENT == ["anna", "anna"]
    with SetWorkflowID(wid):  # a rerun of the same workflow replays the journal
        assert dbos_async.run(notify_auto_twice()) == [
            {"sent_to": "anna", "key": f"{wid}:test.notify:{step}"} for step in (1, 2)
        ]
    assert SENT == ["anna", "anna"]


def test_per_key_dedupes_one_entity_across_workflows(
    dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The action's own name scopes the key; separate workflows see one record."""
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    SENT.clear()
    for _ in range(2):
        with SetWorkflowID(f"per-{uuid4()}"):
            assert dbos_async.run(notify_per("ticket-1")) == {
                "sent_to": "anna",
                "key": "test.notify:ticket-1",
            }
    assert SENT == ["anna"], "the second workflow's step replayed the done record"


def test_per_needs_an_entity() -> None:
    with pytest.raises(ValueError, match="entity"):
        per("")


def test_action_per_arg_builds_and_injects_the_key(
    dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`per=` derives the key from a named argument; callers pass business args only."""
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    SENT.clear()
    for _ in range(2):
        with SetWorkflowID(f"arg-{uuid4()}"):
            assert dbos_async.run(notify_arg_once("anna")) == {
                "sent_to": "anna",
                "key": "test.by_arg:anna",
            }
    assert SENT == ["anna"], "the resolved key reaches the function and the outbox"


def test_per_must_name_an_argument() -> None:
    with pytest.raises(TypeError, match="`per`"):

        @action("test.bad_per", per="nope")
        def bad(idempotency_key: str, to: str) -> dict[str, Any]:  # pragma: no cover
            return {}


def test_retry_policy() -> None:
    assert not retryable(ValueError("bad key"))
    assert not retryable(TypeError("bad result"))
    assert not retryable(OutcomeUnknownError("maybe sent"))
    assert retryable(ActionInProgressError("busy"))
    assert retryable(ConnectionError("receiver down"))


def test_a_permanent_error_is_not_retried() -> None:
    assert not retryable(PermanentError("recipient rejected"))


def test_decorator_applies_its_policy(migrated: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPPD_DATABASE_URL", migrated.database_url)
    k = key()
    with connect(migrated) as c:
        insert(c, k, kind="test.keyless")
        set_status(c, k, "sending", claimed_by="wf-gone", age=600)
    with pytest.raises(OutcomeUnknownError):
        dbos_async.run(keyless(k))


def test_action_in_a_workflow_runs_once_per_key(
    dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    SENT.clear()
    k = key()
    wid = f"notify-{uuid4()}"
    with SetWorkflowID(wid):
        assert dbos_async.run(notify_twice(k)) == [{"sent_to": "anna", "key": k}] * 2
    assert SENT == ["anna"]
    with connect(dbos_runtime) as c:
        assert row(c, k)["claimed_by"] == wid, "the step claims under its workflow's ID"


def test_a_crashed_attempt_of_the_same_workflow_is_recognized(
    dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    k, wid = key(), f"keyless-{uuid4()}"
    with connect(dbos_runtime) as c:
        insert(c, k, kind="test.keyless")
        set_status(c, k, "sending", claimed_by=wid)  # fresh claim: only "own" explains it
    with SetWorkflowID(wid), pytest.raises(OutcomeUnknownError):
        dbos_async.run(keyless_once(k))
    with connect(dbos_runtime) as c:
        assert row(c, k)["status"] == "unknown"


def _unknown(conn: Connection, key: str) -> None:
    run(
        conn,
        sa.insert(ACTIONS).values(
            kind="demo.send",
            idempotency_key=key,
            payload={"to": "anna@example.com"},
            status="unknown",
            last_error="timeout after send",
        ),
    )


def test_list_actions_filters_by_status_and_kind(conn: Connection) -> None:
    key = f"k-{uuid4()}"
    _unknown(conn, key)
    rows = list_actions(conn, status="unknown", kind="demo.send")
    assert key in {r.idempotency_key for r in rows}
    assert all(r.status == "unknown" and r.kind == "demo.send" for r in rows)
    assert key not in {r.idempotency_key for r in list_actions(conn, status="done")}


def test_list_actions_limits(conn: Connection) -> None:
    for _ in range(3):
        _unknown(conn, f"k-{uuid4()}")
    assert len(list_actions(conn, status="unknown", limit=2)) == 2


def test_actions_register_their_names() -> None:
    from mappd import actions, mail  # noqa: PLC0415 - mail registers mappd.send_email

    assert mail.send_email is not None
    assert "mappd.send_email" in actions.NAMES
    assert "test.notify" in actions.NAMES


def test_list_actions_filters_by_claimer(conn: Connection) -> None:
    key, other = f"k-{uuid4()}", f"k-{uuid4()}"
    for k, claimer in ((key, "wf-claimer-1"), (other, "wf-claimer-2")):
        run(
            conn,
            sa.insert(ACTIONS).values(
                kind="demo.send", idempotency_key=k, payload={}, status="done", claimed_by=claimer
            ),
        )
    rows = list_actions(conn, claimed_by="wf-claimer-1")
    assert [r.idempotency_key for r in rows] == [key]
