"""The Telegram Bot API as a channel: parse updates, send messages, fetch files.

A process stores each update keyed by its `update_id` and uses that key as the workflow ID, so
a redelivery (webhook retry, a poll after a crash) is processed once.
Sending has no idempotency key on Telegram's side: a process wraps `send_message` in a durable
action with `on_unknown="escalate"`, so the outbox prevents duplicates and an uncertain send
waits for a person.
"""

import hmac
from typing import TYPE_CHECKING, Any

import httpx2
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Mapping

SECRET_HEADER = "x-telegram-bot-api-secret-token"  # noqa: S105 - a header name
TIMEOUT = 30.0


class Update(BaseModel):
    """The parts of a Telegram update a process uses."""

    update_id: int
    chat_id: int
    text: str = ""
    photo: str | None = None  # file_id of the largest size
    start_token: str | None = None  # from "/start <token>" (a deep link)


def parse(raw: Mapping[str, Any]) -> Update | None:
    """The update's message, or None for updates a bot ignores (edits, channel posts, junk)."""
    update_id = raw.get("update_id")
    message = raw.get("message")
    if not isinstance(update_id, int) or not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
        return None
    text = str(message.get("text") or message.get("caption") or "")
    photos = message.get("photo") or []
    photo = photos[-1].get("file_id") if photos and isinstance(photos[-1], dict) else None
    start = None
    if text.startswith("/start "):
        start = text.removeprefix("/start ").strip() or None
    return Update(
        update_id=update_id, chat_id=chat["id"], text=text, photo=photo, start_token=start
    )


class TelegramError(RuntimeError):
    """Telegram refused a call (`ok: false`) or answered something unusable."""


class TelegramClient:
    """A small synchronous Bot API client; `api_url` points at Telegram or a mock."""

    def __init__(self, token: str, api_url: str, client: httpx2.Client | None = None) -> None:
        """Use bot `token` at `api_url` (no trailing slash)."""
        self._token = token
        self._api = api_url.rstrip("/")
        self._client = client or httpx2.Client(timeout=TIMEOUT)

    def _call(self, method: str, body: dict[str, Any]) -> Any:
        response = self._client.post(f"{self._api}/bot{self._token}/{method}", json=body)
        try:
            data = response.json()
        except ValueError as e:
            raise TelegramError(f"{method}: HTTP {response.status_code}, not JSON") from e
        if not isinstance(data, dict) or not data.get("ok"):
            detail = data.get("description") if isinstance(data, dict) else data
            raise TelegramError(f"{method}: {detail}")
        return data.get("result")

    def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        """Send `text` to `chat_id`; the sent message as Telegram returns it."""
        result = self._call("sendMessage", {"chat_id": chat_id, "text": text})
        return result if isinstance(result, dict) else {}

    def get_updates(self, offset: int, timeout: int = 0) -> list[dict[str, Any]]:
        """Updates with `update_id >= offset` (long polling for up to `timeout` seconds)."""
        result = self._call("getUpdates", {"offset": offset, "timeout": timeout})
        return [u for u in result or [] if isinstance(u, dict)]

    def get_file(self, file_id: str) -> bytes:
        """The bytes of a file a message carried."""
        result = self._call("getFile", {"file_id": file_id})
        path = result.get("file_path") if isinstance(result, dict) else None
        if not path:
            raise TelegramError(f"getFile: no file_path for {file_id!r}")
        response = self._client.get(f"{self._api}/file/bot{self._token}/{path}")
        if response.is_error:
            # Not raise_for_status: its message carries the URL, and the URL the bot token.
            raise TelegramError(f"getFile download: HTTP {response.status_code}")
        return response.content


def verify_secret(headers: Mapping[str, str], expected: str) -> bool:
    """Whether a webhook call carries the secret set with setWebhook; no secret, no access."""
    if not expected:
        return False
    given = {k.lower(): v for k, v in headers.items()}.get(SECRET_HEADER, "")
    return hmac.compare_digest(given, expected)
