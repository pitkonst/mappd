"""Support events acceptance: signed webhooks and an idempotent approval against the stack."""

import time
from typing import Any

import httpx2
import psycopg
import pytest
import stack

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(420)]

TICKETS = f"{stack.APP}/api/v1/support/tickets"


def deliveries() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = httpx2.get(f"{stack.HOOK}/deliveries", timeout=5).json()
    return data


def wait_delivery(case_id: str, type_: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for d in deliveries():
            if (d["case_id"], d["type"]) == (case_id, type_):
                return d
        time.sleep(0.3)
    raise AssertionError(f"no {type_} webhook for {case_id}: {deliveries()}")


def test_webhooks_and_an_idempotent_approval() -> None:
    ticket = {
        "external_id": "w-1",
        "sender": "anna@example.com",
        "subject": "Sensor offline",
        "body": "The temperature sensor in room 4 shows offline.",
    }
    httpx2.post(TICKETS, json=ticket, timeout=10).raise_for_status()
    needed = wait_delivery("w-1", "approval_needed")
    body = {"version": needed["version"], "operator": "olga", "reply": "Please power-cycle it."}
    headers = {"Idempotency-Key": "acceptance-1"}
    first = httpx2.post(f"{TICKETS}/w-1/approve", json=body, headers=headers, timeout=60)
    again = httpx2.post(f"{TICKETS}/w-1/approve", json=body, headers=headers, timeout=60)
    assert first.json() == {"result": "sent", "repeated": False}
    assert again.json() == {"result": "sent", "repeated": True}
    assert len(stack.inbox("anna@example.com")) == 1
    closed = wait_delivery("w-1", "closed")
    assert closed["version"] == needed["version"] + 2
    types = [d["type"] for d in deliveries() if d["case_id"] == "w-1"]
    assert types == ["created", "approval_needed", "closed"], "each event delivered once"


def _deferred(case_id: str) -> int:
    """Deliveries of `case_id` waiting in the outbox for the redelivery sweep."""
    with psycopg.connect(f"{stack.PG}/mappd") as conn:
        row = conn.execute(
            "SELECT count(*) FROM actions WHERE kind = 'mappd.send_webhook'"
            " AND status = 'failed' AND payload->'event'->>'case_id' = %s",
            (case_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def test_a_receiver_outage_is_redelivered_once_it_is_back() -> None:
    """The help desk is down past a delivery's retries; the sweep sends each event later."""
    httpx2.post(f"{stack.HOOK}/down", timeout=5)
    ticket = {
        "external_id": "w-outage",
        "sender": "anna@example.com",
        "subject": "Sensor offline",
        "body": "The temperature sensor in room 9 shows offline.",
    }
    httpx2.post(TICKETS, json=ticket, timeout=10).raise_for_status()
    deadline = time.monotonic() + 90
    while _deferred("w-outage") < 2:  # created and approval_needed, retries spent
        assert time.monotonic() < deadline, "the deliveries never deferred"
        time.sleep(1)
    assert deliveries() == []
    httpx2.post(f"{stack.HOOK}/up", timeout=5)
    # The first redelivery waits a minute after the failure, then the next sweep tick.
    wait_delivery("w-outage", "approval_needed", timeout=180)
    wait_delivery("w-outage", "created", timeout=30)
    assert _deferred("w-outage") == 0
    hits: dict[str, int] = httpx2.get(f"{stack.HOOK}/hits", timeout=5).json()
    assert sorted(hits.values()) == [1, 1], "each event sent once after the outage"
