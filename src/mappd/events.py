"""Case events: what changed, who hears about it, and how webhooks are signed.

A process's workflow calls `emit` after the step that committed a transition. Every event
goes to the in-memory broker (SSE: single node, no replay) and, for each configured webhook
target, to a delivery workflow whose ID comes from the event, so a replayed `emit` delivers
nothing twice.

A delivery retries for seconds, then defers: its outbox row stays `failed`, and the
`mappd.webhook_redeliver` schedule sends it again every so often (waiting as long as it has
been failing, 1 minute to 1 hour) for `REDELIVER_FOR`, then gives up. A receiver down for a
while gets every event once it is back, possibly out of order: events carry `version`.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID
from dbos._error import DBOSMaxStepRetriesExceeded
from pydantic import BaseModel

from mappd import db, ids, telemetry
from mappd.actions import action
from mappd.db.tables import ACTIONS
from mappd.process import Schedule
from mappd.settings import environment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Mapping, Sequence

    from mappd.process import Process

SIGNATURE = "x-mappd-signature"
TOLERANCE = 300


class Event(BaseModel):
    """One transition of one case."""

    id: str
    type: str
    process: str
    case_id: str
    version: int
    occurred_at: datetime
    data: dict[str, Any] = {}

    @classmethod
    def of(
        cls,
        process: str,
        case_id: str,
        type: str,  # noqa: A002 - the event's field name
        version: int,
        data: Mapping[str, Any] | None = None,
    ) -> Event:
        """The event with its stable id "<process>:<case_id>:<type>:<version>"."""
        return cls(
            id=f"{process}:{case_id}:{type}:{version}",
            type=type,
            process=process,
            case_id=case_id,
            version=version,
            occurred_at=datetime.now(UTC),
            data=dict(data or {}),
        )


@dataclass(frozen=True)
class WebhookTarget:
    """Where a process's events go; the URL and secret come from the named env vars."""

    name: str
    url_env: str
    secret_env: str

    def configured(self, env: Mapping[str, str] | None = None) -> bool:
        """Whether both variables are set and non-empty (default: the environment over `.env`)."""
        source = environment() if env is None else env
        return bool(source.get(self.url_env)) and bool(source.get(self.secret_env))


class SignatureError(ValueError):
    """A webhook request is not signed by the shared secret, or is too old."""


def _mac(secret: str, body: bytes, t: int) -> str:
    return hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def sign(secret: str, body: bytes, t: int) -> str:
    """The `X-Mappd-Signature` value for `body` sent at unix time `t`."""
    return f"t={t},v1={_mac(secret, body, t)}"


def verify(
    secret: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    tolerance: int = TOLERANCE,
    now: float | None = None,
) -> None:
    """Check a received webhook; for receivers and tests.

    Raises:
        SignatureError: missing or malformed header, wrong signature, or a timestamp
            further than `tolerance` seconds from `now`.
    """
    value = {k.lower(): v for k, v in headers.items()}.get(SIGNATURE)
    if not value:
        raise SignatureError("missing X-Mappd-Signature")
    try:
        parts = dict(p.split("=", 1) for p in value.split(","))
        t, given = int(parts["t"]), parts["v1"]
    except (KeyError, ValueError) as e:
        raise SignatureError(f"malformed X-Mappd-Signature: {value!r}") from e
    if abs((time.time() if now is None else now) - t) > tolerance:
        raise SignatureError("timestamp outside the tolerance")
    if not hmac.compare_digest(given, _mac(secret, body, t)):
        raise SignatureError("signature does not match")


@dataclass(eq=False)
class _Subscriber:
    process: str | None
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[Event | None]
    dropped: bool = field(default=False)


class Broker:
    """In-memory fan-out of events to SSE subscribers on this node.

    `publish` may run on any thread (workflows run steps on worker threads): it hands each
    event to the subscriber's own loop. A subscriber whose queue is full is dropped; its
    stream ends, and the client reconnects and refetches.
    """

    def __init__(self, maxsize: int = 100) -> None:
        self._maxsize = maxsize
        self._subscribers: set[_Subscriber] = set()

    def publish(self, event: Event) -> None:
        """Offer `event` to every matching subscriber; never blocks."""
        for sub in list(self._subscribers):
            if sub.process in (None, event.process):
                with contextlib.suppress(RuntimeError):  # the subscriber's loop is closed
                    sub.loop.call_soon_threadsafe(self._offer, sub, event)

    def _offer(self, sub: _Subscriber, event: Event) -> None:
        if sub.dropped:
            return
        try:
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            sub.dropped = True
            self._subscribers.discard(sub)
            while not sub.queue.empty():
                sub.queue.get_nowait()
            sub.queue.put_nowait(None)

    async def subscribe(self, process: str | None = None) -> AsyncIterator[Event]:
        """Events of `process` (every process if None) from now on, until dropped."""
        sub = _Subscriber(process, asyncio.get_running_loop(), asyncio.Queue(self._maxsize))
        self._subscribers.add(sub)
        try:
            while (event := await sub.queue.get()) is not None:
                yield event
        finally:
            self._subscribers.discard(sub)


BROKER = Broker()


log = logging.getLogger(__name__)
HTTP = telemetry.traced_client(timeout=10)
RETRYABLE_4XX = frozenset({408, 429})


def delivery_id(event: Event, target: WebhookTarget) -> str:
    """The delivery workflow's ID, and its action's key: one per event and target."""
    return ids.from_key("webhook", f"{event.id}:{target.name}")


async def emit(event: Event, targets: Sequence[WebhookTarget] = ()) -> None:
    """Publish `event` to SSE subscribers and start one delivery per configured target.

    Call it from a workflow after the step that committed the transition. Which targets are
    configured is a journaled step: a replay sees the same answer even if the env changed
    since, so the workflow's step sequence stays the one DBOS recorded. Replays publish to
    SSE again (a refresh, harmless) and start no second delivery: the ID is the event's.
    """
    BROKER.publish(event)
    if not targets:
        return
    names = set(await configured_targets([t.name for t in targets if t.configured()]))
    payload = event.model_dump(mode="json")
    for target in targets:
        if target.name not in names:
            continue
        key = delivery_id(event, target)
        with SetWorkflowID(key):
            await DBOS.start_workflow_async(
                deliver, key, target.url_env, target.secret_env, payload
            )


@DBOS.step(name="mappd.webhook_targets")
async def configured_targets(names: list[str]) -> list[str]:
    """The targets configured when the event was emitted, recorded for replays."""
    return names


@DBOS.workflow(name="mappd.deliver_webhook")
async def deliver(key: str, url_env: str, secret_env: str, event: dict[str, Any]) -> dict[str, Any]:
    """Deliver one event to one target; when the receiver keeps refusing, defer to the sweep.

    `key` is the delivery's own ID, passed in rather than read from the workflow: a
    redelivery runs under a new ID and must settle the same action row. A deferred
    delivery ends in SUCCESS with `deferred` set and its row `failed` — the sweep's queue.
    """
    try:
        return await send_webhook(key, url_env, secret_env, event)
    except (DBOSMaxStepRetriesExceeded, ValueError) as e:  # retries spent, or a refusal
        log.warning("webhook.deferred %s: %s", key, e)
        return {"deferred": f"{type(e).__name__}: {e}"}


REDELIVER_FOR = timedelta(hours=24)
"""How long a failing delivery is sent again before the sweep gives up on it."""
BACKOFF_MIN = timedelta(minutes=1)
BACKOFF_MAX = timedelta(hours=1)
REDELIVER_BATCH = 100
SEND_WEBHOOK = "mappd.send_webhook"


class Due(BaseModel):
    """A failed delivery whose next attempt is due."""

    key: str
    attempts: int
    url_env: str
    secret_env: str
    event: dict[str, Any]


def _failing() -> sa.ColumnElement[bool]:
    return sa.and_(ACTIONS.c.kind == SEND_WEBHOOK, ACTIONS.c.status == "failed")


def _window_start() -> sa.ColumnElement[datetime]:
    return sa.func.now() - sa.bindparam("window", REDELIVER_FOR, type_=sa.Interval)


def due_query(limit: int = REDELIVER_BATCH) -> sa.Select[Any]:
    """Failed deliveries inside the window that have waited as long as they have been failing.

    The wait is clamped to [BACKOFF_MIN, BACKOFF_MAX], so attempts double their spacing up
    to an hour without a counter. A round in progress keeps `updated_at` fresh: not due.
    """
    failing_for = ACTIONS.c.updated_at - ACTIONS.c.created_at
    wait = sa.func.least(
        sa.bindparam("max", BACKOFF_MAX, type_=sa.Interval),
        sa.func.greatest(sa.bindparam("min", BACKOFF_MIN, type_=sa.Interval), failing_for),
    )
    return (
        sa.select(ACTIONS.c.idempotency_key, ACTIONS.c.attempts, ACTIONS.c.payload)
        .where(_failing(), ACTIONS.c.created_at > _window_start())
        .where(ACTIONS.c.updated_at <= sa.func.now() - wait)
        .order_by(ACTIONS.c.updated_at)
        .limit(limit)
    )


def given_up_query() -> sa.Select[Any]:
    """How many deliveries failed past the window (column `given_up`): no longer sent."""
    return (
        sa.select(sa.func.count().label("given_up"))
        .select_from(ACTIONS)
        .where(_failing(), ACTIONS.c.created_at <= _window_start())
    )


@DBOS.step(name="mappd.webhook_due")
async def due_deliveries() -> list[Due]:
    """The deliveries the sweep sends this tick, journaled so a replay starts the same ones."""
    async with db.aconnect() as conn:
        rows = await (await db.arun(conn, due_query())).fetchall()
    return [
        Due(
            key=r["idempotency_key"],
            attempts=r["attempts"],
            url_env=r["payload"]["url_env"],
            secret_env=r["payload"]["secret_env"],
            event=r["payload"]["event"],
        )
        for r in rows
    ]


def redelivery_id(key: str, attempts: int) -> str:
    """One round per failed state: every attempt bumps `attempts`, so the next round is new."""
    return ids.from_key("webhook-redeliver", f"{key}:{attempts}")


@DBOS.workflow(name="mappd.webhook_redeliver")
async def redeliver(scheduled_at: datetime, context: Any) -> None:
    """The sweep's tick: start one short delivery round per due row."""
    del scheduled_at, context
    due_now = await due_deliveries()
    if not due_now:
        telemetry.idle()
    for due in due_now:
        with SetWorkflowID(redelivery_id(due.key, due.attempts)):
            await DBOS.start_workflow_async(
                deliver, due.key, due.url_env, due.secret_env, due.event
            )


def register(processes: Iterable[Process]) -> list[Schedule]:
    """The redelivery sweep, once a minute, when any process declares webhook targets."""
    if not any(p.webhooks for p in processes):
        return []
    return [Schedule(name="mappd.webhooks", workflow=redeliver, cron="0 * * * * *")]


def target_env(url_env: str, secret_env: str) -> tuple[str, str]:
    """A target's URL and secret, read now from the environment over `.env`."""
    env = environment()
    return env.get(url_env, ""), env.get(secret_env, "")


@action("mappd.send_webhook", on_unknown="resend")
def send_webhook(
    idempotency_key: str, url_env: str, secret_env: str, event: dict[str, Any]
) -> dict[str, Any]:
    """POST the signed event. Takes env var *names*: arguments are stored in `actions`.

    5xx, 408, 429 and connection errors are retried by the action; other 4xx are not
    (ValueError). Either way, once the action gives up its row stays `failed` and the
    redelivery sweep sends it again later.
    """
    url, secret = target_env(url_env, secret_env)
    if not url or not secret:
        raise ValueError(f"webhook target is off: set {url_env} and {secret_env}")
    body = json.dumps(event, separators=(",", ":"), sort_keys=True).encode()
    response = HTTP.post(
        url,
        content=body,
        headers={
            "content-type": "application/json",
            "x-mappd-event-id": str(event["id"]),
            "x-mappd-event-type": str(event["type"]),
            "x-mappd-signature": sign(secret, body, int(time.time())),
        },
    )
    code = response.status_code
    if 400 <= code < 500 and code not in RETRYABLE_4XX:  # noqa: PLR2004 - HTTP classes
        raise ValueError(f"webhook {url} answered {code}")
    response.raise_for_status()
    return {"status": code}


def log_unconfigured(process: str, targets: Sequence[WebhookTarget]) -> None:
    """One line per target that will be skipped, at startup rather than per event."""
    for t in targets:
        if not t.configured():
            log.info(
                "webhook target %s.%s is off: set %s and %s",
                process,
                t.name,
                t.url_env,
                t.secret_env,
            )
