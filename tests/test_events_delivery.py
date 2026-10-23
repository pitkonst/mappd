"""Webhook delivery: one signed POST per event and target, retries, and resend by retry."""

import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import dbos_async
import httpx2
import psycopg
import pytest
from dbos import DBOS, SetWorkflowID, WorkflowHandleAsync

from mappd import db, events, telemetry, workflows
from mappd.events import Event, WebhookTarget, delivery_id, emit, verify
from mappd.process import Process

if TYPE_CHECKING:
    from mappd.settings import Settings

FAIL_AFTER = "MAPPD_TEST_FAIL_AFTER_EMIT"


@DBOS.step(name="test.emitting.recorded")
async def recorded() -> str:
    return "recorded"


@DBOS.step(name="test.emitting.flaky", retries_allowed=False)
async def flaky_after_emit() -> str:
    if os.environ.get(FAIL_AFTER):
        raise RuntimeError("down after emit")
    return "ok"


@DBOS.workflow(name="test.emitting")
async def emitting(payload: dict[str, object]) -> str:
    """Emit, record a step, then a step that can fail: the shape of process_ticket."""
    await emit(Event.model_validate(payload), [TARGET])
    await recorded()
    return await flaky_after_emit()


TARGET = WebhookTarget("default", "MAPPD_TEST_HOOK_URL", "MAPPD_TEST_HOOK_SECRET")
SECRET = "hook-secret"  # noqa: S105 - a test fixture


@dataclass
class Receiver:
    """Answers from `replies` in order (200 once they run out); records (code, request)."""

    replies: list[int] = field(default_factory=list)
    seen: list[tuple[int, httpx2.Request]] = field(default_factory=list)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        code = self.replies.pop(0) if self.replies else 200
        self.seen.append((code, request))
        return httpx2.Response(code)

    @property
    def codes(self) -> list[int]:
        return [code for code, _ in self.seen]


@pytest.fixture
def receiver(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> Receiver:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    monkeypatch.setenv(TARGET.url_env, "http://hooks.example/in")
    monkeypatch.setenv(TARGET.secret_env, SECRET)
    r = Receiver()
    client = telemetry.traced_client(transport=httpx2.MockTransport(r.handle))
    monkeypatch.setattr(events, "HTTP", client)
    return r


def event() -> Event:
    return Event.of("support", f"m-{uuid4().hex[:8]}", "approval_needed", 1, {"status": "x"})


def emit_and_settle(e: Event, targets: list[WebhookTarget] | None = None) -> None:
    """Emit, then wait for each delivery in the same loop.

    A delivery is a background workflow on the running loop; dbos_async.run closes its loop
    on return, and a workflow cancelled there takes DBOS's thread pool down with it.
    """
    chosen = [TARGET] if targets is None else targets

    async def go() -> None:
        await emit(e, chosen)
        for t in chosen:
            if t.configured():
                handle: WorkflowHandleAsync[object] = await DBOS.retrieve_workflow_async(
                    delivery_id(e, t)
                )
                with contextlib.suppress(Exception):  # an ERROR delivery is asserted by status
                    await handle.get_result()

    dbos_async.run(go())


def wait_status(settings: Settings, workflow_id: str, want: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = workflows.list_workflows(settings, ids=[workflow_id])
        if found and found[0].status == want:
            return
        time.sleep(0.1)
    raise AssertionError(f"{workflow_id} never reached {want}")


def test_one_signed_post_per_event_and_target(dbos_runtime: Settings, receiver: Receiver) -> None:
    e = event()
    emit_and_settle(e)
    emit_and_settle(e)  # a replayed emit
    wait_status(dbos_runtime, delivery_id(e, TARGET), "SUCCESS")
    assert receiver.codes == [200]
    _, request = receiver.seen[0]
    assert request.headers["x-mappd-event-id"] == e.id
    assert request.headers["x-mappd-event-type"] == "approval_needed"
    verify(SECRET, dict(request.headers), request.content)
    assert json.loads(request.content)["case_id"] == e.case_id


def test_unconfigured_targets_are_skipped(
    dbos_runtime: Settings,
    receiver: Receiver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TARGET.secret_env)
    e = event()
    emit_and_settle(e)
    assert workflows.list_workflows(dbos_runtime, ids=[delivery_id(e, TARGET)]) == []


def test_a_500_is_retried(dbos_runtime: Settings, receiver: Receiver) -> None:
    receiver.replies.append(500)
    e = event()
    emit_and_settle(e)
    wait_status(dbos_runtime, delivery_id(e, TARGET), "SUCCESS")
    assert receiver.codes == [500, 200]


def _action(settings: Settings, key: str) -> tuple[str, int]:
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            "SELECT status, attempts FROM actions WHERE idempotency_key = %s", (key,)
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def _age(settings: Settings, key: str, *, created: str, updated: str) -> None:
    """Move a row back in time instead of waiting for the sweep's backoff."""
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            "UPDATE actions SET created_at = now() - %s::interval,"
            " updated_at = now() - %s::interval WHERE idempotency_key = %s",
            (created, updated, key),
        )


def _sweep(settings: Settings) -> list[str]:
    """Run one tick of the redelivery sweep and wait for the rounds it started."""
    keys = {d.key: d.attempts for d in dbos_async.run(events.due_deliveries())}

    async def go() -> None:
        handle = await DBOS.start_workflow_async(events.redeliver, datetime.now(UTC), None)
        await handle.get_result()
        for key, attempts in keys.items():
            child: WorkflowHandleAsync[object] = await DBOS.retrieve_workflow_async(
                events.redelivery_id(key, attempts)
            )
            await child.get_result()

    dbos_async.run(go())
    return sorted(keys)


def test_a_404_defers_and_the_sweep_delivers_later(
    dbos_runtime: Settings, receiver: Receiver
) -> None:
    receiver.replies.append(404)
    e = event()
    wid = delivery_id(e, TARGET)
    emit_and_settle(e)
    wait_status(dbos_runtime, wid, "SUCCESS")
    assert receiver.codes == [404], "a 4xx is not retried in the round"
    assert _action(dbos_runtime, wid)[0] == "failed", "the failed row is the sweep's queue"
    assert _sweep(dbos_runtime) == [], "not due before its backoff"
    _age(dbos_runtime, wid, created="10 minutes", updated="5 minutes")
    assert wid in _sweep(dbos_runtime)
    assert receiver.codes == [404, 200]
    assert _action(dbos_runtime, wid)[0] == "done", "the resend settles the same row"
    _age(dbos_runtime, wid, created="20 minutes", updated="10 minutes")
    assert wid not in _sweep(dbos_runtime), "a done delivery is never sent again"
    assert receiver.codes == [404, 200]


def test_spent_retries_defer_instead_of_failing_the_workflow(
    dbos_runtime: Settings, receiver: Receiver
) -> None:
    receiver.replies.extend([500] * 5)
    e = event()
    wid = delivery_id(e, TARGET)
    emit_and_settle(e)
    wait_status(dbos_runtime, wid, "SUCCESS", timeout=60)
    assert receiver.codes == [500] * 5
    assert _action(dbos_runtime, wid) == ("failed", 5)


def test_the_sweep_picks_due_rows_inside_the_window(dbos_runtime: Settings) -> None:
    """Wait as long as the row has been failing, 1 minute to 1 hour, for 24 hours."""
    rows = {
        # name: (created ago, updated ago)
        "fresh": ("30 seconds", "10 seconds"),  # waits its first minute
        "first-retry": ("2 minutes", "70 seconds"),  # failing 50 s, waited > 1 min
        "backing-off": ("40 minutes", "10 minutes"),  # failing 30 min, waited 10
        "doubled": ("40 minutes", "20 minutes"),  # failing 20 min, waited 20
        "capped": ("10 hours", "61 minutes"),  # failing 9 h, the wait caps at 1 h
        "given-up": ("25 hours", "2 hours"),  # past the window
    }
    tag = uuid4().hex[:8]
    with db.connect(dbos_runtime) as conn:
        for name, (created, updated) in rows.items():
            conn.execute(
                "INSERT INTO actions (kind, idempotency_key, payload, status, attempts,"
                " created_at, updated_at) VALUES ('mappd.send_webhook', %s,"
                ' \'{"url_env": "U", "secret_env": "S", "event": {}}\','
                " 'failed', 1, now() - %s::interval, now() - %s::interval)",
                (f"{tag}-{name}", created, updated),
            )
        due = db.run(conn, events.due_query(limit=10_000)).fetchall()
        given_up = db.run(conn, events.given_up_query()).fetchone()
    keys = [r["idempotency_key"] for r in due]
    picked = {k.removeprefix(f"{tag}-") for k in keys if k.startswith(tag)}
    assert picked == {"first-retry", "doubled", "capped"}
    assert given_up is not None
    assert given_up["given_up"] >= 1


def test_the_sweep_is_scheduled_only_with_webhook_targets() -> None:
    assert events.register([Process(name="quiet")]) == []
    [schedule] = events.register([Process(name="loud", webhooks=[TARGET])])
    assert schedule.workflow is events.redeliver
    assert schedule.cron == "0 * * * * *", "once a minute"


def test_payload_holds_env_names_not_the_secret(dbos_runtime: Settings, receiver: Receiver) -> None:
    e = event()
    emit_and_settle(e)
    wait_status(dbos_runtime, delivery_id(e, TARGET), "SUCCESS")
    with psycopg.connect(dbos_runtime.database_url) as conn:
        row = conn.execute(
            "SELECT payload::text FROM actions WHERE idempotency_key = %s",
            (delivery_id(e, TARGET),),
        ).fetchone()
    assert row is not None
    assert SECRET not in row[0]
    assert "hooks.example" not in row[0]
    assert TARGET.secret_env in row[0]


def test_emit_publishes_to_the_broker(receiver: Receiver) -> None:
    async def scenario() -> Event:
        stream = events.BROKER.subscribe("support")
        first = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        await emit(event())
        return await asyncio.wait_for(first, 2)

    assert dbos_async.run(scenario()).process == "support"


def test_turning_webhooks_on_does_not_break_a_retry(
    dbos_runtime: Settings, receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The targets an emit used are journaled: a replay must see the same ones."""
    monkeypatch.delenv(TARGET.url_env)
    monkeypatch.setenv(FAIL_AFTER, "1")
    wid = f"emitting-{uuid4()}"

    async def run_once() -> None:
        with SetWorkflowID(wid):
            handle = await DBOS.start_workflow_async(emitting, event().model_dump(mode="json"))
        with contextlib.suppress(RuntimeError):
            await handle.get_result()

    dbos_async.run(run_once())
    wait_status(dbos_runtime, wid, "ERROR")
    monkeypatch.setenv(TARGET.url_env, "http://hooks.example/in")  # webhooks turned on
    monkeypatch.delenv(FAIL_AFTER)
    moved = workflows.retry(dbos_runtime, wid)
    wait_status(dbos_runtime, moved.new_workflow_id, "SUCCESS")
    assert receiver.codes == [], "the event was emitted while webhooks were off"
