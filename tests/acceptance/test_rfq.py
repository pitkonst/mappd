"""RFQ acceptance: an RFQ is quoted, approved once and emailed, on the stack's mocks."""

import time
from typing import Any

import httpx2
import psycopg
import pytest
import stack

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(300)]
REQUESTS = f"{stack.APP}/api/v1/rfq/requests"


def wait(ext: str, status: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        r = httpx2.get(f"{REQUESTS}/{ext}", timeout=5)
        if r.status_code == 200:
            last = r.json()
            if last["status"] == status:
                return last
        time.sleep(0.3)
    raise AssertionError(f"{ext} never reached {status}: {last}")


def journal(workflow_id: str) -> list[str]:
    with psycopg.connect(f"{stack.PG}/mappd_dbos") as conn:
        rows = conn.execute(
            "SELECT function_name FROM dbos.operation_outputs WHERE workflow_uuid = %s",
            (workflow_id,),
        ).fetchall()
    return [r[0] for r in rows]


def test_an_rfq_is_quoted_approved_and_emailed_once() -> None:
    httpx2.post(
        REQUESTS,
        json={
            "external_id": "q-1",
            "sender": "anna@example.com",
            "subject": "Request for quotation",
            "body": "Please quote 12 x Temperature Sensor T100 and 1 x Helio Hub 400.",
        },
        timeout=10,
    ).raise_for_status()
    rfq = wait("q-1", "awaiting_approval")
    out = rfq["output"]
    assert out["outcome"] == "quote"
    assert {(line["sku"], line["quantity"]) for line in out["lines"]} == {
        ("SEN-T100", 12),
        ("CTL-H400", 1),
    }
    assert out["total"] == "1266.00"
    body = {"version": rfq["version"], "operator": "olga", "reply": out["draft"]}
    headers = {"Idempotency-Key": "acc-rfq-1"}
    first = httpx2.post(f"{REQUESTS}/q-1/approve", json=body, headers=headers, timeout=60)
    again = httpx2.post(f"{REQUESTS}/q-1/approve", json=body, headers=headers, timeout=60)
    assert first.json() == {"result": "sent", "repeated": False}
    assert again.json()["repeated"] is True
    (sent,) = stack.inbox("anna@example.com")
    assert "1266.00" in sent.text
    assert sent.subject == "Request for quotation"
    steps = journal("rfq.request:q-1")
    assert "rfq.extractor__model.request" in steps
    assert not [s for s in steps if s.startswith("support.")]
