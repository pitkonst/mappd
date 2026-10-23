"""Support acceptance: nine scenarios against the compose stack."""

import concurrent.futures as cf
import json
import time
from typing import TYPE_CHECKING, Any

import httpx2
import psycopg
import pytest
import stack

if TYPE_CHECKING:
    from collections.abc import Callable

    from mappd.channels.email import Email

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(900)]

TICKETS = f"{stack.APP}/api/v1/support/tickets"


def post_ticket(ext: str, subject: str, body: str, sender: str = "anna@example.com") -> str:
    r = httpx2.post(
        TICKETS,
        json={"external_id": ext, "sender": sender, "subject": subject, "body": body},
        timeout=10,
    )
    r.raise_for_status()
    return ext


def ticket(ext: str) -> dict[str, Any]:
    response = httpx2.get(f"{TICKETS}/{ext}", timeout=5)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data


def wait_status(ext: str, status: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = ticket(ext)
            if last["status"] == status:
                return last
        except httpx2.HTTPError:
            pass
        time.sleep(0.3)
    raise AssertionError(f"ticket {ext} never reached {status}: {last}")


def calls() -> dict[str, int]:
    data: dict[str, int] = httpx2.get(f"{stack.LLM}/calls", timeout=5).json()
    return data


def wait_for(probe: Callable[[], bool], what: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def drafting() -> bool:
    return calls().get("draft_search", 0) >= 1


def delivered(address: str) -> Callable[[], bool]:
    return lambda: len(stack.inbox(address)) >= 1


def emails() -> list[Email]:
    return stack.inbox("anna@example.com")


def journal(workflow_id: str) -> list[str]:
    with psycopg.connect(f"{stack.PG}/mappd_dbos") as conn:
        rows = conn.execute(
            "SELECT function_name FROM dbos.operation_outputs WHERE workflow_uuid = %s"
            " ORDER BY function_id",
            (workflow_id,),
        ).fetchall()
    return [r[0] for r in rows]


def workflow_status(workflow_id: str) -> str:
    with psycopg.connect(f"{stack.PG}/mappd_dbos") as conn:
        row = conn.execute(
            "SELECT status FROM dbos.workflow_status WHERE workflow_uuid = %s", (workflow_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def approve(
    ext: str, version: int, reply: str = "Please power-cycle the controller (kb-101)."
) -> httpx2.Response:
    return httpx2.post(
        f"{TICKETS}/{ext}/approve",
        json={"version": version, "operator": "olga", "reply": reply},
        timeout=60,
    )


def test_happy_path_and_journal() -> None:
    post_ticket("m-1", "Sensor offline", "The temperature sensor shows offline since yesterday.")
    t = wait_status("m-1", "awaiting_approval")
    assert t["output"]["route"] == "technical"
    assert t["output"]["outcome"] == "reply"
    assert t["output"]["draft"]["citations"], "the draft must cite the knowledge base"
    assert approve("m-1", t["version"]).json()["result"] == "sent"
    wait_status("m-1", "sent")
    assert len(emails()) == 1
    # Model requests are DBOS steps; the read-only search_kb tool is not.
    # Webhooks are on in the acceptance stack: each event journals its configured targets,
    # then starts one delivery child workflow per target.
    assert journal("support.ticket:m-1") == [
        "support.load_ticket",
        "mappd.webhook_targets",
        "mappd.deliver_webhook",
        "support.classifier__model.request",
        "support.drafter__model.request",
        "support.drafter__model.request",
        "support.save_outcome",
        "mappd.webhook_targets",
        "mappd.deliver_webhook",
        "support.autonomy_level",  # a reply ticket reads its category's level (journaled)
    ]


def test_refund_is_escalated_by_rule() -> None:
    post_ticket("m-2", "Refund", "I want my money back for the cabinet, refund please.")
    t = wait_status("m-2", "escalated")
    assert (t["output"]["outcome"], t["output"]["escalate_to"]) == ("escalate", "sales")
    assert t["output"]["draft"] is None
    assert calls().get("draft_search", 0) == 0


def test_double_click_approval_sends_once() -> None:
    post_ticket("m-3", "Sensor offline", "Sensor offline in room 2.")
    version = wait_status("m-3", "awaiting_approval")["version"]
    with cf.ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: approve("m-3", version), range(4)))
    assert {r.status_code for r in results} == {200}
    assert len(emails()) == 1


def test_stale_version_is_rejected() -> None:
    post_ticket("m-4", "Sensor offline", "Sensor offline in room 3.")
    version = wait_status("m-4", "awaiting_approval")["version"]
    assert approve("m-4", version).status_code == 200
    assert approve("m-4", version - 1).status_code == 409
    assert len(emails()) == 1


def test_duplicate_delivery_processed_once() -> None:
    """A redelivered ticket (a help desk retrying its webhook) collapses into one workflow."""
    httpx2.post(f"{stack.LLM}/delay/draft/3", timeout=5)  # keep the first run in flight

    def deliver(_: int) -> str:
        return post_ticket("m-dup", "Firmware", "controller firmware update failed")

    with cf.ThreadPoolExecutor(3) as pool:
        assert set(pool.map(deliver, range(3))) == {"m-dup"}
    httpx2.post(f"{stack.LLM}/delay/draft/0", timeout=5)
    wait_status("m-dup", "awaiting_approval")
    assert calls()["classify"] == 1


def test_crash_mid_draft_recovers_without_repeating_finished_steps() -> None:
    httpx2.post(f"{stack.LLM}/delay/draft/4", timeout=5)
    post_ticket("m-6", "Sensor offline", "Sensor offline in room 6.")
    wait_for(drafting, "the draft request")  # classify done, draft request in flight
    assert calls()["classify"] == 1
    stack.kill()
    httpx2.post(f"{stack.LLM}/delay/draft/0", timeout=5)
    stack.start("v1")  # same version: DBOS recovers it itself
    wait_status("m-6", "awaiting_approval")
    c = calls()
    assert c["classify"] == 1, "the finished model step comes from the journal"
    assert c["draft_search"] == 2, "the interrupted step re-runs (at-least-once)"


def test_deploy_graceful_then_approve_on_new_version() -> None:
    httpx2.post(f"{stack.LLM}/delay/draft/3", timeout=5)
    post_ticket("m-8", "Sensor offline", "Sensor offline in room 8.")
    wait_for(drafting, "the draft request")
    drained = stack.stop_graceful()  # docker compose up -d sends SIGTERM
    httpx2.post(f"{stack.LLM}/delay/draft/0", timeout=5)
    assert workflow_status("support.ticket:m-8") == "SUCCESS", "SIGTERM lets it finish"
    assert drained < 15
    stack.start("v2")
    t = wait_status("m-8", "awaiting_approval")
    assert approve("m-8", t["version"]).json()["result"] == "sent"
    assert len(emails()) == 1


def test_hard_crash_then_new_version_needs_recover() -> None:
    httpx2.post(f"{stack.LLM}/delay/draft/4", timeout=5)
    post_ticket("m-9", "Sensor offline", "Sensor offline in room 9.")
    wait_for(drafting, "the draft request")
    stack.kill()
    httpx2.post(f"{stack.LLM}/delay/draft/0", timeout=5)
    stack.start("v2")
    time.sleep(3)
    assert ticket("m-9")["status"] == "new", "v2 does not recover a v1 workflow on its own"
    out = stack.compose("exec", "-T", "app", "mappd", "workflow", "recover", "--json", version="v2")
    moved = json.loads(out.stdout)["moved"]
    # Scheduled polls queued for v1 before the crash are stranded too and move with it.
    assert {m["old_version"] for m in moved} == {"v1"}
    assert "support.ticket:m-9" in {m["workflow_id"] for m in moved}
    wait_status("m-9", "awaiting_approval")
    assert calls()["classify"] == 1, "the fork reuses the journal of finished steps"


def test_a_slow_send_does_not_freeze_the_app() -> None:
    # slow-smtp answers a slow* recipient 5 s after it delivered the message.
    post_ticket(
        "m-10", "Sensor offline", "Sensor offline in room 10.", sender="slow-anna@example.com"
    )
    version = wait_status("m-10", "awaiting_approval")["version"]
    with cf.ThreadPoolExecutor(1) as pool:
        decision = pool.submit(approve, "m-10", version)
        wait_for(delivered("slow-anna@example.com"), "the email to be delivered")
        started = time.monotonic()
        httpx2.get(f"{stack.APP}/health", timeout=10).raise_for_status()
        assert time.monotonic() - started < 1, "a step's I/O must not block the event loop"
        assert decision.result().json()["result"] == "sent"


def test_the_app_comes_back_by_itself_after_it_exits() -> None:
    before = stack.started_at()
    # A process exit Docker did not ask for (docker kill counts as a manual stop).
    stack.compose(
        "exec", "-T", "app", "python", "-c", "import os, signal; os.kill(1, signal.SIGTERM)"
    )
    wait_for(lambda: stack.started_at() != before, "Docker to restart the app", timeout=60)
    stack.wait_healthy()
