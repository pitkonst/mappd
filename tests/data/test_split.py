from collections import Counter

from mappd.data.cases import Case
from mappd.data.split import split


def case(n: int, persona: str, outcome: str) -> Case:
    return Case(
        id=f"s-{n}",
        input={},
        expected={"outcome": outcome},
        meta={"persona": persona, "slot": outcome, "trap": None},
    )


CASES = [
    case(i, f"p{i % 40}", ("reply", "reply", "clarify", "escalate", "escalate")[i % 5])
    for i in range(200)
]


def test_personas_never_cross_and_only_holdout_has_the_canary() -> None:
    dev, holdout = split(CASES, 0.2, seed=7, canary="MAPPD-CANARY-x")
    assert {c.meta["persona"] for c in dev}.isdisjoint({c.meta["persona"] for c in holdout})
    assert all(c.meta["canary"] == "MAPPD-CANARY-x" for c in holdout)
    assert all("canary" not in c.meta for c in dev)
    assert len(dev) + len(holdout) == len(CASES)


def test_holdout_is_close_to_its_share_and_keeps_the_mix() -> None:
    _dev, holdout = split(CASES, 0.2, seed=7, canary="c")
    assert 0.15 <= len(holdout) / len(CASES) <= 0.3

    def shares(cases: list[Case]) -> dict[str, float]:
        counts = Counter(c.expected["outcome"] for c in cases)
        return {k: v / len(cases) for k, v in counts.items()}

    for outcome, share in shares(CASES).items():
        assert abs(shares(holdout).get(outcome, 0) - share) < 0.15


def test_one_dominant_persona_overshoots_but_never_crosses() -> None:
    lopsided = [case(i, "big", "reply") for i in range(80)] + [
        case(100 + i, f"p{i}", "reply") for i in range(20)
    ]
    dev, holdout = split(lopsided, 0.2, seed=1, canary="c")
    assert {c.meta["persona"] for c in dev}.isdisjoint({c.meta["persona"] for c in holdout})


def test_split_is_deterministic() -> None:
    assert split(CASES, 0.2, seed=3, canary="c") == split(CASES, 0.2, seed=3, canary="c")


def test_rare_strata_are_spread_across_both_sides() -> None:
    traps = ["t1", "t2", "t3", "t4", "t5"]
    cases = []
    for p in range(60):
        outcome = ("reply", "clarify", "escalate")[p % 3]
        trap = traps[p % 5] if p % 4 == 0 else None
        for k in range(2):
            c = case(p * 2 + k, f"p{p}", outcome)
            cases.append(c.model_copy(update={"meta": {**c.meta, "trap": trap}}))
    for seed in range(5):
        _dev, holdout = split(cases, 0.2, seed=seed, canary="c")

        def share(group: list[Case], key: str, value: object) -> float:
            hits = sum(
                bool((c.meta["trap"] if key == "trap" else c.expected["outcome"]) == value)
                for c in group
            )
            return hits / len(group)

        for outcome in ("reply", "clarify", "escalate"):
            assert abs(share(holdout, "outcome", outcome) - share(cases, "outcome", outcome)) <= 0.1
        assert abs(share(holdout, "trap", None) - share(cases, "trap", None)) <= 0.1
