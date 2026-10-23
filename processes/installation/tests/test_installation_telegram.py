"""Sending to Telegram: a call that may have reached the bot is an unknown outcome, not a retry."""

from typing import TYPE_CHECKING

import httpx2
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from mappd.actions import OutcomeUnknownError
from mappd.channels.telegram import TelegramClient
from processes.installation import telegram


def failing(error: Exception) -> TelegramClient:
    def handle(request: httpx2.Request) -> httpx2.Response:
        raise error

    return TelegramClient(
        "tok", "http://tg", client=httpx2.Client(transport=httpx2.MockTransport(handle))
    )


@pytest.mark.parametrize(
    "error",
    [
        httpx2.ReadTimeout("slow"),
        httpx2.WriteTimeout("slow"),
        httpx2.RemoteProtocolError("dropped"),
    ],
)
def test_a_send_that_may_have_landed_is_unknown(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(telegram, "client", lambda: failing(error))
    with pytest.raises(OutcomeUnknownError):
        telegram.deliver(1, "hi")


def test_a_send_that_never_connected_stays_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telegram, "client", lambda: failing(httpx2.ConnectError("refused")))
    with pytest.raises(httpx2.ConnectError):
        telegram.deliver(1, "hi")


def test_helpers_read_the_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`mappd serve` does not export .env; the helpers still find the file's values."""
    env_file = tmp_path / "app.env"
    env_file.write_text(
        "MAPPD_INSTALLATION_TELEGRAM_SECRET=file-secret\nMAPPD_INSTALLATION_TELEGRAM_MODE=webhook\n"
    )
    monkeypatch.setenv("MAPPD_ENV_FILE", str(env_file))
    monkeypatch.delenv("MAPPD_INSTALLATION_TELEGRAM_SECRET", raising=False)
    monkeypatch.delenv("MAPPD_INSTALLATION_TELEGRAM_MODE", raising=False)
    assert telegram.secret() == "file-secret"
    assert telegram.mode() == "webhook"
    monkeypatch.setenv("MAPPD_INSTALLATION_TELEGRAM_MODE", "polling")
    assert telegram.mode() == "polling", "the process env wins over the file"
