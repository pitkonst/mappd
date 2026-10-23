from mappd.autonomy import Readiness
from processes.rfq.trust import rates

READY = Readiness(without_edits=0.9, false_escalations_max=0.1, min_cases=3)


def row(**kw: int) -> dict[str, int]:
    return {
        "proposals": 0,
        "auto": 0,
        "without_edits": 0,
        "resolutions": 0,
        "false_escalations": 0,
        **kw,
    }


def test_ready_suggests_the_rfq_command() -> None:
    t = rates(row(proposals=10, without_edits=10), "review", READY, "quote")
    assert t.verdict == "ready for auto"
    assert t.command == "mappd process autonomy rfq quote auto"


def test_too_few_cases() -> None:
    assert (
        rates(row(proposals=2, without_edits=2), "review", READY, "clarify").verdict
        == "too few cases"
    )
