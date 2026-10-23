from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from mappd import actions, workflow_view
from mappd.db import transaction
from mappd.workflow_view import (
    PAYLOAD_LIMIT,
    allowed,
    error_text,
    ms,
    payload,
    process_of,
    step_from,
    step_kind,
    summary,
)


class Reply(BaseModel):
    to: str
    body: str


def test_payload_renders_json_pretty() -> None:
    p = payload({"args": ["t-1"], "kwargs": {}})
    assert p.text == '{\n  "args": [\n    "t-1"\n  ],\n  "kwargs": {}\n}'
    assert not p.truncated


def test_payload_renders_a_string_as_itself() -> None:
    assert payload("sent").text == "sent"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"\xff", "\\xff"),
        (ValueError("x"), "ValueError: x"),
        (Reply(to="anna@example.com", body="Hi"), '"to": "anna@example.com"'),
        ({1, 2}, "1"),
        ("a" * 5_000_000, "aaaa"),
    ],
)
def test_payload_handles_non_json_and_huge_values(value: object, expected: str) -> None:
    p = payload(value)
    assert expected in p.text
    assert len(p.text) <= PAYLOAD_LIMIT
    if isinstance(value, str):
        assert p.truncated
        assert len(p.text) == PAYLOAD_LIMIT


def test_error_text_names_the_exception() -> None:
    assert error_text(RuntimeError("down")) == "RuntimeError: down"
    assert error_text("plain") == "plain"
    assert error_text(None) is None


def test_summary_takes_the_first_line_and_caps_it() -> None:
    assert summary("first\nsecond") == "first"
    assert summary("x" * 500) == "x" * 200
    assert summary(None) is None
    assert summary("") is None


def test_ms_converts_epoch_milliseconds_to_utc() -> None:
    assert ms(0) == datetime(1970, 1, 1, tzinfo=UTC)
    assert ms(1_500) == datetime(1970, 1, 1, 0, 0, 1, 500_000, tzinfo=UTC)
    assert ms(None) is None


@pytest.mark.parametrize(
    ("name", "process"),
    [("support.process_ticket", "support"), ("mappd.mail_poll", "mappd"), ("bare", "")],
)
def test_process_of_splits_the_name(name: str, process: str) -> None:
    assert process_of(name) == process


def test_step_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions, "NAMES", {"x.send"})
    monkeypatch.setattr(transaction, "TRANSACTIONS", {"x.save"})
    assert step_kind("x.send", None) == "action"
    assert step_kind("x.save", None) == "transaction"
    assert step_kind("x.child", "child-1") == "child"
    assert step_kind("x.classify", None) == "step"


def test_steps_without_timings_have_no_duration() -> None:
    step = step_from(
        {
            "function_id": 1,
            "function_name": "x.first",
            "output": "one",
            "error": None,
            "child_workflow_id": None,
            "started_at_epoch_ms": None,
            "completed_at_epoch_ms": None,
        },
        payloads=True,
    )
    assert step.duration_ms is None
    assert step.started_at is None
    assert step.output is not None
    assert step.output.text == "one"


def test_step_from_measures_and_hides() -> None:
    info = {
        "function_id": 2,
        "function_name": "x.second",
        "output": "secret",
        "error": RuntimeError("boom"),
        "child_workflow_id": None,
        "started_at_epoch_ms": 1_000,
        "completed_at_epoch_ms": 1_250,
    }
    step = step_from(info, payloads=False)
    assert step.duration_ms == 250
    assert step.output is None, "payloads off hides step outputs"
    assert step.error == "RuntimeError: boom", "errors stay"


@pytest.mark.parametrize(
    ("status", "stranded", "current", "expected"),
    [
        ("PENDING", False, True, ["cancel"]),
        ("ENQUEUED", True, True, ["cancel", "recover"]),
        ("PENDING", True, False, ["cancel", "recover"]),
        ("ERROR", False, True, ["retry", "fork"]),
        ("SUCCESS", False, True, ["fork"]),
        ("CANCELLED", False, True, ["resume", "fork"]),
        ("CANCELLED", False, False, ["fork"]),
        ("MAX_RECOVERY_ATTEMPTS_EXCEEDED", False, True, ["resume", "fork"]),
    ],
)
def test_allowed_actions_by_status(
    status: str, stranded: bool, current: bool, expected: list[str]
) -> None:
    assert allowed(status, stranded=stranded, current_version=current) == expected


def test_statuses_split_into_finished_and_in_flight() -> None:
    assert set(workflow_view.FINISHED).isdisjoint(workflow_view.IN_FLIGHT)
