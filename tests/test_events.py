import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from mappd.events import Broker, Event, SignatureError, WebhookTarget, sign, target_env, verify

if TYPE_CHECKING:
    from pathlib import Path

SECRET = "s3cret"  # noqa: S105 - a test fixture
BODY = b'{"id":"support:m-1:closed:3"}'


def test_event_id_is_stable() -> None:
    e = Event.of("support", "m-1", "closed", 3, {"status": "sent"})
    assert e.id == "support:m-1:closed:3"
    assert e.occurred_at.tzinfo is not None


def test_sign_and_verify() -> None:
    header = sign(SECRET, BODY, 1_700_000_000)
    assert header.startswith("t=1700000000,v1=")
    verify(SECRET, {"X-Mappd-Signature": header}, BODY, now=1_700_000_100)


@pytest.mark.parametrize(
    ("secret", "body", "now", "match"),
    [
        ("wrong", BODY, 1_700_000_000, "signature"),
        (SECRET, BODY + b" ", 1_700_000_000, "signature"),
        (SECRET, BODY, 1_700_000_301, "timestamp"),
    ],
)
def test_verify_rejects(secret: str, body: bytes, now: int, match: str) -> None:
    header = sign(SECRET, BODY, 1_700_000_000)
    with pytest.raises(SignatureError, match=match):
        verify(secret, {"x-mappd-signature": header}, body, now=now)


def test_verify_rejects_a_missing_or_malformed_header() -> None:
    with pytest.raises(SignatureError):
        verify(SECRET, {}, BODY)
    with pytest.raises(SignatureError):
        verify(SECRET, {"x-mappd-signature": "garbage"}, BODY)


def test_target_is_configured_only_with_both_vars() -> None:
    t = WebhookTarget("default", "U", "S")
    assert t.configured({"U": "http://x", "S": "k"})
    assert not t.configured({"U": "http://x"})
    assert not t.configured({"U": "", "S": "k"})


def test_a_target_set_in_dotenv_is_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `mappd serve` on the host reads .env like every other setting, not only os.environ.
    env_file = tmp_path / ".env"
    env_file.write_text("MAPPD_T_URL=http://hooks.example/in\nMAPPD_T_SECRET=k\n")
    monkeypatch.setenv("MAPPD_ENV_FILE", str(env_file))
    assert WebhookTarget("default", "MAPPD_T_URL", "MAPPD_T_SECRET").configured()
    assert target_env("MAPPD_T_URL", "MAPPD_T_SECRET") == ("http://hooks.example/in", "k")


async def _take(broker: Broker, process: str | None, n: int) -> list[Event]:
    got: list[Event] = []
    async for event in broker.subscribe(process):
        got.append(event)
        if len(got) == n:
            break
    return got


def test_subscribers_get_their_process_only() -> None:
    async def scenario() -> list[Event]:
        broker = Broker()
        task = asyncio.create_task(_take(broker, "support", 1))
        await asyncio.sleep(0)
        broker.publish(Event.of("rfq", "r-1", "created", 0))
        broker.publish(Event.of("support", "m-1", "created", 0))
        return await asyncio.wait_for(task, 2)

    [event] = asyncio.run(scenario())
    assert event.process == "support"


def test_publish_from_another_thread_reaches_the_subscriber() -> None:
    async def scenario() -> list[Event]:
        broker = Broker()
        task = asyncio.create_task(_take(broker, None, 1))
        await asyncio.sleep(0)
        thread = threading.Thread(
            target=broker.publish, args=(Event.of("support", "m-1", "created", 0),)
        )
        thread.start()
        thread.join()
        return await asyncio.wait_for(task, 2)

    assert len(asyncio.run(scenario())) == 1


def test_a_slow_subscriber_is_dropped_without_blocking_publish() -> None:
    async def scenario() -> list[Event]:
        broker = Broker(maxsize=2)
        got: list[Event] = []

        async def consume() -> None:
            async for e in broker.subscribe(None):
                got.append(e)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)  # subscribed, waiting on its queue
        for v in range(5):  # all five offers run before the consumer resumes: overflow
            broker.publish(Event.of("support", "m-1", "created", v))
        await asyncio.wait_for(task, 2)  # the stream ends: the subscriber was dropped
        return got

    assert asyncio.run(scenario()) == []
