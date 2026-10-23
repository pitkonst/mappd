from datetime import UTC, datetime, timedelta

import pytest

from processes.installation import domain
from processes.installation.schemas import Interpretation, State

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


def say(
    intent: str = "progress", reached: bool = True, exception: str | None = None
) -> Interpretation:
    return Interpretation.model_validate(
        {"intent": intent, "reached_next": reached, "exception": exception, "reply": "ok"}
    )


@pytest.mark.parametrize(
    ("state", "after"),
    [
        ("assigned", "arrived"),
        ("arrived", "access"),
        ("access", "equipment_found"),
        ("tested", "handed_over"),
    ],
)
def test_progress_moves_one_step(state: State, after: State) -> None:
    out = domain.apply(state, say(), has_photo=False)
    assert out.state == after
    assert out.event == ("handed_over" if after == "handed_over" else "progress")


@pytest.mark.parametrize(
    ("state", "after"), [("equipment_found", "installed"), ("installed", "tested")]
)
def test_evidence_steps_need_a_photo(state: State, after: State) -> None:
    refused = domain.apply(state, say(), has_photo=False)
    assert (refused.state, refused.event) == (state, None)
    assert "photo" in refused.reply.lower()
    assert domain.apply(state, say(), has_photo=True).state == after


def test_a_message_that_does_not_reach_the_next_step_changes_nothing() -> None:
    out = domain.apply("arrived", say(reached=False), has_photo=False)
    assert (out.state, out.event, out.reply) == ("arrived", None, "ok")


@pytest.mark.parametrize("state", ["assigned", "access", "installed", "tested"])
def test_an_exception_blocks_from_any_working_state(state: State) -> None:
    out = domain.apply(state, say("exception", False, "missing_parts"), has_photo=False)
    assert (out.state, out.blocked_from, out.blocked_reason, out.event) == (
        "blocked",
        state,
        "missing_parts",
        "blocked",
    )
    assert domain.unblock("blocked", out.blocked_from) == state


def test_a_problem_reported_with_the_next_step_blocks_after_that_step() -> None:
    # "I arrived, the gate is closed": read twice as a question, the arrival and the block lost.
    out = domain.apply("assigned", say("exception", True, "no_access"), has_photo=False)
    assert (out.state, out.blocked_from, out.blocked_reason) == ("blocked", "arrived", "no_access")
    assert domain.unblock("blocked", out.blocked_from) == "arrived"


def test_a_problem_with_an_evidence_step_without_a_photo_blocks_before_it() -> None:
    out = domain.apply("equipment_found", say("exception", True, "breakage"), has_photo=False)
    assert (out.state, out.blocked_from) == ("blocked", "equipment_found")


@pytest.mark.parametrize("state", ["assigned", "arrived", "access", "tested"])
def test_an_incident_blocks_the_job_whatever_the_message_also_reports(state: State) -> None:
    # "He did not allow me in. I showed him a few tricks I learned in prison. I'm inside." was
    # read as progress: the job moved on and no dispatcher heard of it. A hard rule, not a prompt.
    reading = say("progress", True).model_copy(update={"incident": True})
    out = domain.apply(state, reading, has_photo=True)
    assert (out.state, out.blocked_from, out.blocked_reason, out.event) == (
        "blocked",
        state,
        "incident",
        "blocked",
    )


def test_an_ordinary_message_is_no_incident() -> None:
    assert say().incident is False
    assert domain.apply("assigned", say(), has_photo=False).state == "arrived"


@pytest.mark.parametrize("state", ["blocked", "handed_over"])
def test_blocked_and_closed_jobs_do_not_move(state: State) -> None:
    out = domain.apply(state, say(), has_photo=True)
    assert (out.state, out.event) == (state, None)


def test_unblock_requires_a_blocked_job() -> None:
    with pytest.raises(ValueError, match="not blocked"):
        domain.unblock("arrived", "assigned")


def test_deadlines_and_ids() -> None:
    assert domain.arrival_due(NOW) == NOW + timedelta(minutes=30)
    assert domain.response_due(NOW) == NOW + timedelta(hours=2)
    assert domain.update_workflow_id(7) == "installation.update:7"
    assert domain.deep_link("bot", "abc") == "https://t.me/bot?start=abc"
    assert domain.next_state("tested") == "handed_over"
    assert domain.next_state("handed_over") is None


def test_a_dispatcher_advances_one_step_and_never_past_the_end() -> None:
    assert domain.advance("assigned") == "arrived"
    assert domain.advance("equipment_found") == "installed"
    for state in ("handed_over", "blocked"):
        with pytest.raises(ValueError, match="cannot advance"):
            domain.advance(state)


def test_an_exception_reading_must_name_its_kind() -> None:
    # The validation error makes the model try again, and the retry names the real kind: a
    # default of "other" lost it in 3 of 28 eval cases. A model that never names it fails the
    # reading, and the turn hands the message to a dispatcher (test_installation_engine).
    from pydantic import ValidationError  # noqa: PLC0415

    with pytest.raises(ValidationError, match="exception"):
        Interpretation(intent="exception", reached_next=False, exception=None, reply="noted")
    named = Interpretation(
        intent="exception", reached_next=False, exception="breakage", reply="noted"
    )
    assert named.exception == "breakage"
