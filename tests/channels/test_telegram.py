import json
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

from mappd.channels.telegram import TelegramClient, TelegramError, Update, parse, verify_secret

if TYPE_CHECKING:
    from collections.abc import Callable


def update(**message: Any) -> dict[str, Any]:
    return {"update_id": 10, "message": {"message_id": 1, "chat": {"id": 501}, **message}}


def test_parse_text() -> None:
    assert parse(update(text="I have arrived")) == Update(
        update_id=10, chat_id=501, text="I have arrived", photo=None, start_token=None
    )


def test_parse_a_captioned_photo_takes_the_largest_size() -> None:
    raw = update(caption="installed", photo=[{"file_id": "small"}, {"file_id": "large"}])
    got = parse(raw)
    assert got is not None
    assert (got.text, got.photo) == ("installed", "large")


def test_parse_start_token() -> None:
    got = parse(update(text="/start abc123"))
    assert got is not None
    assert got.start_token == "abc123"  # noqa: S105 - a test token


@pytest.mark.parametrize(
    "raw",
    [
        {"update_id": 11, "edited_message": {"chat": {"id": 1}, "text": "x"}},
        {"update_id": 12, "channel_post": {"chat": {"id": 1}, "text": "x"}},
        {"update_id": 13},
        {"message": {"chat": {"id": 1}}},
        {"update_id": "x", "message": {"chat": {"id": 1}}},
    ],
)
def test_ignored_and_malformed_updates(raw: dict[str, Any]) -> None:
    assert parse(raw) is None


def client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> tuple[TelegramClient, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    http = httpx2.Client(transport=httpx2.MockTransport(record))
    return TelegramClient("tok", "http://tg", client=http), seen


def test_send_message_posts_to_the_bot_url() -> None:
    tg, seen = client(
        lambda _r: httpx2.Response(200, json={"ok": True, "result": {"message_id": 5}})
    )
    assert tg.send_message(501, "hi") == {"message_id": 5}
    assert str(seen[0].url) == "http://tg/bottok/sendMessage"
    assert json.loads(seen[0].content) == {"chat_id": 501, "text": "hi"}


def test_a_refused_call_raises() -> None:
    tg, _ = client(
        lambda _r: httpx2.Response(200, json={"ok": False, "description": "chat not found"})
    )
    with pytest.raises(TelegramError, match="chat not found"):
        tg.send_message(1, "x")


def test_get_updates_and_get_file() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx2.Response(200, json={"ok": True, "result": [update(text="a")]})
        if request.url.path.endswith("/getFile"):
            return httpx2.Response(200, json={"ok": True, "result": {"file_path": "photos/p.jpg"}})
        return httpx2.Response(200, content=b"JPEG")

    tg, seen = client(handler)
    assert tg.get_updates(7)[0]["update_id"] == 10
    assert json.loads(seen[0].content)["offset"] == 7
    assert tg.get_file("p") == b"JPEG"
    assert str(seen[-1].url) == "http://tg/file/bottok/photos/p.jpg"


def test_verify_secret() -> None:
    header = "X-Telegram-Bot-Api-Secret-Token"
    assert verify_secret({header: "s3"}, "s3")
    assert verify_secret({header.lower(): "s3"}, "s3")
    assert not verify_secret({header: "no"}, "s3")
    assert not verify_secret({}, "s3")
    assert not verify_secret({header: ""}, "")


def test_a_failed_download_names_no_token() -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/getFile"):
            return httpx2.Response(200, json={"ok": True, "result": {"file_path": "p/1.jpg"}})
        return httpx2.Response(404)

    http = httpx2.Client(transport=httpx2.MockTransport(handle))
    bot = TelegramClient("secret-token", "http://tg", client=http)
    with pytest.raises(TelegramError) as raised:
        bot.get_file("f-1")
    assert "secret-token" not in str(raised.value)
    assert "HTTP 404" in str(raised.value)
