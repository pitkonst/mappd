from typing import TYPE_CHECKING

from mappd.data.cases import Case, read_cases
from mappd.feedback import merge

if TYPE_CHECKING:
    from pathlib import Path


def case(i: str, reply: str = "reply", edited: bool = False) -> Case:
    return Case(
        id=f"fb-{i}",
        input={"external_id": i},
        expected={"outcome": reply},
        meta={"source": "feedback", "edited": edited},
    )


def test_sync_merges_by_id(tmp_path: Path) -> None:
    path = tmp_path / "feedback.jsonl"
    first = merge(path, [case("1"), case("2", edited=True)])
    assert (first.new, first.updated, first.unchanged, first.edited) == (2, 0, 0, 1)
    second = merge(path, [case("1"), case("2", "escalate"), case("3")])
    assert (second.new, second.updated, second.unchanged) == (1, 1, 1)
    by_id = {c.id: c for c in read_cases(path)}
    assert by_id["fb-2"].expected == {"outcome": "escalate"}
    assert len(by_id) == 3


def test_merge_leaves_no_temp_file(tmp_path: Path) -> None:
    merge(tmp_path / "feedback.jsonl", [case("1")])
    assert [p.name for p in tmp_path.iterdir()] == ["feedback.jsonl"]
