import random
from collections import Counter

import pytest

from mappd.data.plan import MixError, make_slots, parse_mix

KINDS = ("reply", "clarify", "escalate")
TRAPS = {"refund_without_word": "escalate", "passing_refund": "reply", "german": "reply"}


def test_parse_mix() -> None:
    assert parse_mix("reply=.5,clarify=.2,escalate=.3", KINDS) == {
        "reply": 0.5,
        "clarify": 0.2,
        "escalate": 0.3,
    }


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("reply=.5,clarify=.2", "sum to 1"),
        ("reply=.5,clarify=.2,shout=.3", "unknown outcome 'shout'"),
        ("reply=1.2,clarify=-.2", "negative"),
        ("reply", "name=share"),
    ],
)
def test_bad_mixes_are_rejected(text: str, message: str) -> None:
    with pytest.raises(MixError, match=message):
        parse_mix(text, KINDS)


def test_slots_follow_the_mix_exactly() -> None:
    slots = make_slots(
        10, {"reply": 0.5, "clarify": 0.2, "escalate": 0.3}, TRAPS, 0.0, random.Random(1)
    )
    assert Counter(s.kind for s in slots) == {"reply": 5, "clarify": 2, "escalate": 3}
    assert [s.index for s in slots] == list(range(10))


def test_traps_match_their_kind_and_rate() -> None:
    slots = make_slots(
        100, {"reply": 0.5, "clarify": 0.2, "escalate": 0.3}, TRAPS, 0.15, random.Random(2)
    )
    trapped = [s for s in slots if s.trap]
    assert len(trapped) == 15
    assert all(TRAPS[s.trap] == s.kind for s in trapped if s.trap)
    assert {s.trap for s in trapped} == set(TRAPS)


def test_slots_are_deterministic() -> None:
    mix = {"reply": 0.5, "clarify": 0.2, "escalate": 0.3}
    assert make_slots(50, mix, TRAPS, 0.2, random.Random(3)) == make_slots(
        50, mix, TRAPS, 0.2, random.Random(3)
    )
