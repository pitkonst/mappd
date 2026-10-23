"""Workflow dashboard acceptance: fork a sent reply, still one email."""

import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx2
import pytest
import stack

if TYPE_CHECKING:
    from collections.abc import Callable

    from mappd.channels.email import Email

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(600)]

TICKETS = f"{stack.APP}/api/v1/support/tickets"
WORKFLOWS = f"{stack.APP}/api/v1/workflows"


def until[T](probe: Callable[[], T | None], what: str, timeout: float = 60) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            found = probe()
        except httpx2.HTTPError:
            found = None
        if found:
            return found
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what}")


def ticket(mid: str, status: str) -> dict[str, Any]:
    seen: list[object] = []

    def probe() -> dict[str, Any] | None:
        r = httpx2.get(f"{TICKETS}/{quote(mid, safe='')}", timeout=5)
        data: dict[str, Any] = r.json() if r.status_code == 200 else {}
        seen.append(data.get("status", r.status_code))
        return data if data.get("status") == status else None

    try:
        return until(probe, f"ticket {mid} {status}")
    except AssertionError as e:
        raise AssertionError(f"{e}; last seen: {seen[-3:]}") from e


def replies() -> list[Email]:
    return stack.inbox("customer@mappd.test")


def workflow(wid: str, status: str | None = None) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        r = httpx2.get(f"{WORKFLOWS}/{quote(wid, safe='')}", timeout=10)
        data: dict[str, Any] = r.json() if r.status_code == 200 else {}
        return data if data and (status is None or data["status"] == status) else None

    return until(probe, f"workflow {wid} {status or ''}")


def test_forking_a_sent_reply_sends_nothing_again() -> None:
    mid = stack.send_mail(
        "support@mappd.test", subject="Sensor offline", body="The sensor in room 7 is offline."
    )
    t = ticket(mid, "awaiting_approval")
    approved = httpx2.post(
        f"{TICKETS}/{quote(mid, safe='')}/approve",
        json={"version": t["version"], "operator": "olga", "reply": "Please restart it."},
        timeout=60,
    )
    assert approved.json()["result"] == "sent"
    until(lambda: replies() or None, "the reply")

    rows = httpx2.get(
        WORKFLOWS, params={"name": "support.approve_and_send", "status": "SUCCESS"}, timeout=10
    ).json()
    [original] = [r for r in rows if mid in r["workflow_id"]]
    detail = workflow(original["workflow_id"], "SUCCESS")
    [send] = [s for s in detail["steps"] if s["name"] == "mappd.send_email"]
    [ref] = [a for a in detail["actions"] if a["kind"] == "mappd.send_email"]
    assert not ref["per_workflow"], "the send is keyed by the case, not the workflow"
    assert ref["status"] == "done"

    fork = httpx2.post(
        f"{WORKFLOWS}/{quote(original['workflow_id'], safe='')}/fork",
        json={"start_step": send["function_id"]},
        timeout=30,
    )
    assert fork.status_code == 200, fork.text
    forked = workflow(fork.json()["new_workflow_id"], "SUCCESS")
    assert forked["forked_from"] == original["workflow_id"]

    stats = httpx2.get(f"{WORKFLOWS}/stats", params={"window": "1h"}, timeout=10).json()
    assert stats["by_name"]["support.approve_and_send"] >= 2
    time.sleep(5)  # GreenMail would have the second reply by now if the fork had sent one
    assert len(replies()) == 1
    page = httpx2.get(f"{stack.APP}/workflows/{quote(forked['workflow_id'], safe='')}", timeout=10)
    assert page.status_code == 200
    assert "mappd.send_email" in page.text
