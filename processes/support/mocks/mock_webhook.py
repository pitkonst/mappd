"""Stand-in for a help desk receiving support webhooks: verifies, deduplicates, records.

The secret is MAPPD_SUPPORT_WEBHOOK_SECRET (the app signs with the same variable). `/down`
makes it answer 503 until `/up`, to stage an outage; `/hits` counts every accepted POST per
event ID, duplicates included, so a test can tell "delivered once" from "deduplicated".
"""

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from mappd.events import SignatureError, verify
from mappd.settings import environment

app = FastAPI(title="mock-webhook")
PORT = 9200
DELIVERIES: list[dict[str, Any]] = []
SEEN: set[str] = set()
HITS: dict[str, int] = {}
STATE = {"down": False}


def _secret() -> str:
    return environment().get("MAPPD_SUPPORT_WEBHOOK_SECRET") or "dev-webhook-secret"


@app.post("/hook")
async def hook(request: Request) -> dict[str, str]:
    """Accept a signed event once per event ID; 503 while down."""
    if STATE["down"]:
        raise HTTPException(503, "down for the test")
    body = await request.body()
    try:
        verify(_secret(), dict(request.headers), body)
    except SignatureError as e:
        raise HTTPException(401, str(e)) from e
    event = json.loads(body)
    HITS[event["id"]] = HITS.get(event["id"], 0) + 1
    if event["id"] in SEEN:
        return {"status": "duplicate_ignored"}
    SEEN.add(event["id"])
    DELIVERIES.append({k: event.get(k) for k in ("id", "type", "case_id", "version")})
    return {"status": "ok"}


@app.get("/deliveries")
def deliveries() -> list[dict[str, Any]]:
    """Every distinct event received since the last reset, in arrival order."""
    return DELIVERIES


@app.get("/hits")
def hits() -> dict[str, int]:
    """Accepted POSTs per event ID since the last reset, duplicates included."""
    return HITS


@app.post("/down")
def down() -> dict[str, Any]:
    """Answer 503 to every delivery until `/up`."""
    STATE["down"] = True
    return {}


@app.post("/up")
def up() -> dict[str, Any]:
    """Accept deliveries again."""
    STATE["down"] = False
    return {}


@app.post("/reset")
def reset() -> dict[str, Any]:
    """Forget everything and come back up."""
    DELIVERIES.clear()
    SEEN.clear()
    HITS.clear()
    STATE["down"] = False
    return {}
