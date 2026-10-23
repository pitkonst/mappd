import asyncio
import smtplib
import time
from email.message import EmailMessage

from mappd.mocks.slow_smtp import Relay


def message(to: str) -> EmailMessage:
    m = EmailMessage()
    m["From"] = "support@mappd.test"
    m["To"] = to
    m["Subject"] = "Hello"
    m["Message-ID"] = "<s1@mappd.test>"
    m.set_content(".starts with a dot\nsecond line")
    return m


def send(port: int, to: str) -> float:
    started = time.monotonic()
    with smtplib.SMTP("127.0.0.1", port, local_hostname="test", timeout=10) as client:
        client.login("support@mappd.test", "any")
        client.send_message(message(to))
    return time.monotonic() - started


async def scenario() -> tuple[Relay, float, float]:
    sink = Relay(upstream=None)
    sink_server = await asyncio.start_server(sink.handle, "127.0.0.1", 0)
    sink_port = sink_server.sockets[0].getsockname()[1]
    relay = Relay(upstream=("127.0.0.1", sink_port), delay=0.5)
    relay_server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
    relay_port = relay_server.sockets[0].getsockname()[1]
    async with sink_server, relay_server:
        fast = await asyncio.to_thread(send, relay_port, "anna@example.com")
        slow = await asyncio.to_thread(send, relay_port, "slow-anna@example.com")
    return sink, fast, slow


def test_the_relay_forwards_and_delays_slow_recipients() -> None:
    sink, fast, slow = asyncio.run(scenario())
    assert [rcpts for _, rcpts, _ in sink.received] == [
        ["anna@example.com"],
        ["slow-anna@example.com"],
    ]
    assert fast < 0.4
    assert slow >= 0.5
    sender, _, data = sink.received[0]
    assert sender == "support@mappd.test"
    assert b"Message-ID: <s1@mappd.test>" in data
    assert b"\r\n.starts with a dot\r\n" in data, "dot-stuffing is undone before forwarding"


async def refused() -> str:
    relay = Relay(upstream=("127.0.0.1", 1))  # nothing listens on port 1
    server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        try:
            await asyncio.to_thread(send, port, "anna@example.com")
        except smtplib.SMTPDataError as e:
            return f"{e.smtp_code}"
    return "sent"


def test_an_unreachable_upstream_answers_451() -> None:
    assert asyncio.run(refused()) == "451"
