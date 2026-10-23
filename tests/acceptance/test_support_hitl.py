"""Support HITL acceptance: shadow mode, an auto category, a false escalation, feedback sync."""

import json
import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
import stack

from mappd.data.cases import read_cases

if TYPE_CHECKING:
    from pathlib import Path

    from mappd.channels.email import Email

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(600)]
TICKETS = f"{stack.APP}/api/v1/support/tickets"
REVIEW = "shadow: false\ndefault: review\n"


def post(ext: str, subject: str, body: str) -> None:
    httpx2.post(
        TICKETS,
        json={"external_id": ext, "sender": "anna@example.com", "subject": subject, "body": body},
        timeout=10,
    ).raise_for_status()


def wait(ext: str, *statuses: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        r = httpx2.get(f"{TICKETS}/{ext}", timeout=5)
        if r.status_code == 200:
            last = r.json()
            if last["status"] in statuses:
                return last
        time.sleep(0.3)
    raise AssertionError(f"{ext} never reached {statuses}: {last}")


def emails() -> list[Email]:
    return stack.inbox("anna@example.com")


def test_shadow_mode_sends_nothing() -> None:
    stack.set_policy("shadow: true\ndefault: review\n")
    post("h-1", "Sensor offline", "The temperature sensor shows offline since yesterday.")
    t = wait("h-1", "awaiting_approval")
    r = httpx2.post(
        f"{TICKETS}/h-1/approve",
        timeout=60,
        json={"version": t["version"], "operator": "olga", "reply": "Power-cycle it."},
    )
    assert r.json()["result"] == "shadowed"
    assert wait("h-1", "shadowed")["decision"]["shadow"] is True
    assert emails() == []


def test_an_auto_category_is_sent_without_approval() -> None:
    # mock_llm classifies a ticket mentioning a sensor as malfunction (TECH_WORDS)
    stack.set_policy("shadow: false\ndefault: review\ncategories:\n  malfunction: auto\n")
    post("h-2", "Sensor offline", "The temperature sensor shows offline since yesterday.")
    t = wait("h-2", "sent")
    assert t["decision"]["operator"] == "auto"
    assert len(emails()) == 1


def test_a_false_escalation_counts_and_feedback_syncs(tmp_path: Path) -> None:
    stack.set_policy(REVIEW)
    post("h-3", "Refund", "I want my money back for the cabinet, refund please.")
    t = wait("h-3", "escalated")
    r = httpx2.post(
        f"{TICKETS}/h-3/resolve",
        timeout=60,
        json={
            "version": t["version"],
            "operator": "olga",
            "agent_could_handle": True,
            "outcome": "reply",
        },
    )
    assert r.json()["result"] == "resolved"
    trust = httpx2.get(f"{stack.APP}/api/v1/support/trust", timeout=10).json()
    assert trust["total"]["false_escalations"] == 1
    # A developer's command: run on the host against the stack's database, into a temp file
    # (the app image's code directory is read-only for the app user, as it should be).
    target = tmp_path / "feedback.jsonl"
    out = subprocess.run(  # noqa: S603 - fixed argv
        [
            sys.executable,
            "-m",
            "mappd",
            "feedback",
            "sync",
            "support",
            "--out",
            str(target),
            "--json",
        ],
        env={**os.environ, "MAPPD_DATABASE_URL": f"{stack.PG}/mappd"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout)["new"] == 1
    [case] = read_cases(target)
    assert (case.expected["outcome"], case.meta["kind"]) == ("reply", "resolve")
