"""The installation bot: settings from env, the client, and the durable send.

Telegram has no idempotency key, so `send_message` is an outbox action that escalates an
uncertain send to a person (`/actions`) instead of risking a duplicate.
"""

from typing import Any

import httpx2

from mappd import durable
from mappd.actions import OutcomeUnknownError
from mappd.channels.telegram import TelegramClient
from mappd.settings import environment

DEFAULT_API = "https://api.telegram.org"


def token() -> str:
    """The bot token (empty: the channel is off)."""
    return environment().get("MAPPD_INSTALLATION_TELEGRAM_TOKEN", "")


def mode() -> str:
    """`polling`, `webhook` or `off` (default)."""
    return environment().get("MAPPD_INSTALLATION_TELEGRAM_MODE", "off") or "off"


def secret() -> str:
    """The webhook secret Telegram sends in `X-Telegram-Bot-Api-Secret-Token`."""
    return environment().get("MAPPD_INSTALLATION_TELEGRAM_SECRET", "")


def bot_key() -> str:
    """Which bot an offset belongs to: the token's bot id (the part before the colon).

    Each bot numbers its own updates, so a new bot or token must not inherit an old offset;
    the secret part of the token is never stored.
    """
    return token().split(":", 1)[0] or "default"


def bot_name() -> str:
    """The bot's username, for deep links."""
    return environment().get("MAPPD_INSTALLATION_BOT_NAME", "") or "helio_install_bot"


def client() -> TelegramClient:
    """A client for the configured bot (tests replace this)."""
    api = environment().get("MAPPD_INSTALLATION_TELEGRAM_API_URL", "") or DEFAULT_API
    return TelegramClient(token(), api)


# Failures after the request may have reached Telegram: resending could duplicate the message.
MAYBE_SENT = (httpx2.ReadTimeout, httpx2.WriteTimeout, httpx2.RemoteProtocolError)


def deliver(chat_id: int, text: str) -> dict[str, Any]:
    """Send `text` now.

    Raises:
        OutcomeUnknownError: the request may have been delivered (a timeout after sending).
    """
    try:
        return client().send_message(chat_id, text)
    except MAYBE_SENT as e:
        raise OutcomeUnknownError(f"sendMessage: {type(e).__name__}") from e


@durable.action(per="key", on_unknown="escalate")
def send_message(idempotency_key: str, key: str, chat_id: int, text: str) -> dict[str, Any]:
    """Send `text` once per `key` (an update's reply, a deadline's reminder)."""
    return deliver(chat_id, text)
