"""Installation acceptance: installation jobs driven by Telegram updates polled from the mock."""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

import httpx2
import psycopg
import pytest
import stack

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(300)]
JOBS = f"{stack.APP}/api/v1/installation/jobs"
UPDATE_IDS = count(1000)


def create(ext: str) -> str:
    """A job for a new installer; returns the deep link's start token."""
    r = httpx2.post(
        JOBS,
        json={
            "external_id": ext,
            "installer_name": f"Installer {ext}",
            "site": "Riverside 4",
            "skus": ["CTL-H400"],
            "appointment_at": datetime.now(UTC).isoformat(),
        },
        timeout=10,
    )
    r.raise_for_status()
    link = r.json()["deep_link"]
    assert "?start=" in link
    return str(link.split("?start=", 1)[1])


def push(chat: int, text: str = "", photo: str | None = None, update_id: int | None = None) -> int:
    uid = update_id if update_id is not None else next(UPDATE_IDS)
    message: dict[str, Any] = {"message_id": uid, "chat": {"id": chat}, "text": text}
    if photo:
        message["photo"] = [{"file_id": photo, "width": 90, "height": 90}]
    body = {"update_id": uid, "message": message}
    httpx2.post(f"{stack.TELEGRAM}/test/push", json=body, timeout=5).raise_for_status()
    return uid


def sent(chat: int) -> list[str]:
    msgs = httpx2.get(f"{stack.TELEGRAM}/test/sent", timeout=5).json()
    return [m["text"] for m in msgs if m["chat_id"] == chat]


def wait_sent(chat: int, n: int, timeout: float = 60) -> list[str]:
    """Wait until the bot has sent `n` messages to `chat` (polling runs every 5 s)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(got := sent(chat)) >= n:
            return got
        time.sleep(0.3)
    raise AssertionError(f"chat {chat} got {sent(chat)}, expected {n} messages")


def job(ext: str) -> dict[str, Any]:
    r = httpx2.get(f"{JOBS}/{ext}", timeout=5)
    r.raise_for_status()
    return dict(r.json())


def say(chat: int, text: str, photo: str | None = None) -> str:
    """Send one message and return the bot's reply to it."""
    before = len(sent(chat))
    push(chat, text, photo)
    return wait_sent(chat, before + 1)[-1]


def linked(ext: str, chat: int) -> None:
    token = create(ext)
    assert "linked" in say(chat, f"/start {token}").lower()


def test_a_new_installer_links_their_chat() -> None:
    token = create("inst-1")
    reply = say(501, f"/start {token}")
    assert "linked" in reply.lower()
    assert job("inst-1")["deep_link"] is None


def test_a_job_walks_to_handed_over_with_photos() -> None:
    linked("inst-2", 502)
    steps = [
        ("I have arrived", None, "arrived"),
        ("access granted", None, "access"),
        ("equipment found", None, "equipment_found"),
    ]
    for text, photo, state in steps:
        say(502, text, photo)
        assert job("inst-2")["state"] == state
    assert "photo" in say(502, "installed").lower()
    assert job("inst-2")["state"] == "equipment_found"
    for text, state in [("installed", "installed"), ("tested", "tested")]:
        say(502, text, photo="photo-1")
        assert job("inst-2")["state"] == state
    say(502, "handed over to the customer")
    final = job("inst-2")
    assert final["state"] == "handed_over"
    assert any(m["photo_description"] for m in final["messages"] if m["direction"] == "in")


def test_a_redelivered_update_is_processed_once() -> None:
    linked("inst-3", 503)
    before = len(sent(503))
    uid = push(503, "I have arrived")
    push(503, "I have arrived", update_id=uid)
    wait_sent(503, before + 1)
    time.sleep(8)  # more than a polling period: a second reply would have landed by now
    assert len(sent(503)) == before + 1
    assert job("inst-3")["state"] == "arrived"
    assert job("inst-3")["version"] == 1, "one transition"


def test_missing_parts_block_until_the_dispatcher_unblocks() -> None:
    linked("inst-4", 504)
    say(504, "I have arrived")
    say(504, "parts are missing")
    blocked = job("inst-4")
    assert (blocked["state"], blocked["blocked_reason"]) == ("blocked", "missing_parts")
    before = len(sent(504))
    r = httpx2.post(
        f"{JOBS}/inst-4/unblock",
        json={"version": blocked["version"], "operator": "olga"},
        timeout=30,
    )
    assert r.status_code == 200
    assert r.json()["state"] == "arrived"
    assert "cleared" in wait_sent(504, before + 1)[-1].lower()
    time.sleep(1)
    assert len(sent(504)) == before + 1


def timeline(ext: str) -> list[dict[str, Any]]:
    r = httpx2.get(f"{JOBS}/{ext}/timeline", timeout=5)
    r.raise_for_status()
    return list(r.json())


def test_a_dispatcher_takes_over_and_hands_back() -> None:
    linked("inst-5", 505)
    held = httpx2.post(
        f"{JOBS}/inst-5/takeover",
        json={"operator": "olga", "version": job("inst-5")["version"]},
        timeout=10,
    )
    assert held.status_code == 200
    before = len(sent(505))
    r = httpx2.post(
        f"{JOBS}/inst-5/messages",
        json={"operator": "olga", "text": "I am calling the site manager now."},
        headers={"Idempotency-Key": "acc-msg-1"},
        timeout=30,
    )
    assert r.status_code == 200
    assert wait_sent(505, before + 1)[-1] == "I am calling the site manager now."
    push(505, "I have arrived")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        texts = [m["text"] for m in job("inst-5")["messages"] if m["direction"] == "in"]
        if "I have arrived" in texts:
            break
        time.sleep(0.5)
    else:
        raise AssertionError("the held message was never recorded")
    time.sleep(6)  # more than a polling period: a reply would have landed
    assert len(sent(505)) == before + 1, "no reply while a dispatcher holds the conversation"
    assert job("inst-5")["state"] == "assigned"
    released = httpx2.post(
        f"{JOBS}/inst-5/release",
        json={"operator": "olga", "version": held.json()["version"]},
        timeout=10,
    )
    assert released.status_code == 200
    say(505, "I am on site now, arrived")
    assert job("inst-5")["state"] == "arrived"
    kinds = [e["event"] for e in timeline("inst-5") if e["kind"] == "event"]
    assert kinds[:2] == ["takeover", "release"]
    authors = [e["actor"] for e in timeline("inst-5") if e["kind"] == "message"]
    assert "dispatcher:olga" in authors


def test_a_confirmed_proposal_reserves_once_through_the_gateway() -> None:
    from processes.installation import domain  # noqa: PLC0415

    linked("inst-6", 506)
    say(506, "I have arrived")
    say(506, "access granted")
    before = len(sent(506))
    uid = push(506, "parts are missing: no controller in the delivery")
    reply = wait_sent(506, before + 1)[-1]
    assert "dispatcher" in reply.lower()
    blocked = job("inst-6")
    assert (blocked["state"], blocked["blocked_reason"]) == ("blocked", "missing_parts")
    (proposal,) = blocked["proposals"]
    assert (proposal["tool"], proposal["status"]) == ("reserve_parts", "pending")
    with psycopg.connect(f"{stack.PG}/mappd_dbos") as conn:
        steps = [
            r[0]
            for r in conn.execute(
                "SELECT function_name FROM dbos.operation_outputs WHERE workflow_uuid = %s",
                (domain.update_workflow_id(uid),),
            ).fetchall()
        ]
    assert any(s.endswith(".call_tool") for s in steps), f"MCP calls are journaled: {steps}"
    for _ in range(2):
        r = httpx2.post(
            f"{JOBS}/inst-6/proposals/{proposal['id']}/confirm",
            json={"operator": "olga"},
            timeout=60,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "done"
    calls = httpx2.get(f"{stack.MCP}/test/calls", timeout=5).json()
    assert [c["tool"] for c in calls].count("reserve_parts") == 1
    assert "check_stock" in [c["tool"] for c in calls]
    confirmations = [t for t in wait_sent(506, before + 2) if "reserved" in t.lower()]
    assert len(confirmations) == 1
    assert job("inst-6")["state"] == "blocked", "confirming does not unblock"
    events = [e["event"] for e in timeline("inst-6") if e["kind"] == "event"]
    order = ["blocked", "resolution", "tool_call", "proposal", "confirmed", "done"]
    positions = [events.index(kind) for kind in order]
    assert positions == sorted(positions), events


def test_a_simulation_eval_runs_through_the_stack(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "MAPPD_GATEWAY_URL": "http://127.0.0.1:14000/v1",
        "MAPPD_DATABASE_URL": f"{stack.PG}/mappd",
        "MAPPD_MLFLOW_URL": "http://127.0.0.1:15001",
    }
    run = subprocess.run(  # noqa: S603 - fixed argv
        [
            sys.executable,
            "-m",
            "mappd",
            "eval",
            "run",
            "installation",
            "--limit",
            "3",
            "--json",
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        env=env,
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode in {0, 1}, run.stderr
    report = json.loads(run.stdout)
    assert report["logged"], run.stderr
    assert (report["cases"], report["errored"]) == (3, 0)
    assert {m["name"] for m in report["metrics"]} >= {"final_state", "timely_escalation"}
