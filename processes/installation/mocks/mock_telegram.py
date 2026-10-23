"""A stand-in for the Telegram Bot API: updates to poll, messages sent, one photo to download.

Test hooks push updates (the same `update_id` twice included, as a redelivery) and list what
the bot sent.
"""

import itertools
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Response

app = FastAPI(title="mock-telegram")
PORT = 9300
UPDATES: list[dict[str, Any]] = []
SENT: list[dict[str, Any]] = []
# Every file is this photo: a controller on a wall, its light green (as the mock vision says).
JPEG = (Path(__file__).parent / "photo.jpg").read_bytes()
# Ids for pushes that leave them out. They start at the clock, so they stay above the offset an
# app saved before this mock restarted, and a demo can be run again without picking ids.
NEXT_ID = itertools.count(int(time.time()))


def _ok(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


@app.post("/bot{token}/getUpdates")
def get_updates(token: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:  # noqa: B008
    """Updates with `update_id >= offset`, in the order they were pushed."""
    offset = int(body.get("offset") or 0)
    return _ok([u for u in UPDATES if int(u.get("update_id", 0)) >= offset])


@app.post("/bot{token}/sendMessage")
def send_message(token: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    """Record a message the bot sends."""
    SENT.append({"chat_id": body.get("chat_id"), "text": body.get("text")})
    return _ok({"message_id": len(SENT)})


@app.post("/bot{token}/getFile")
def get_file(token: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    """Every file is the same small photo."""
    return _ok({"file_id": body.get("file_id"), "file_path": f"photos/{body.get('file_id')}.jpg"})


@app.get("/file/bot{token}/photos/{name}")
def download(token: str, name: str) -> Response:
    """The photo's bytes."""
    if not name.endswith(".jpg"):
        raise HTTPException(404)
    return Response(JPEG, media_type="image/jpeg")


@app.post("/test/push")
def push(update: dict[str, Any] = Body(...)) -> dict[str, int]:  # noqa: B008
    """Queue an update for polling (or for a test to post to the webhook).

    Without an `update_id` (and a message without a `message_id`) the mock assigns fresh
    ones; an explicit `update_id` is kept, so a test can push the same one twice.
    """
    update.setdefault("update_id", next(NEXT_ID))
    message = update.get("message")
    if isinstance(message, dict):
        message.setdefault("message_id", update["update_id"])
    UPDATES.append(update)
    return {"queued": len(UPDATES), "update_id": int(update["update_id"])}


@app.get("/test/sent")
def sent() -> list[dict[str, Any]]:
    """Every message the bot sent since the last reset."""
    return SENT


@app.post("/test/reset")
def reset() -> dict[str, Any]:
    """Forget updates and sent messages."""
    UPDATES.clear()
    SENT.clear()
    return {}
