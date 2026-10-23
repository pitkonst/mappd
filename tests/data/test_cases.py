from typing import TYPE_CHECKING

import pytest

from mappd.data.cases import Case, CaseFormatError, read_cases, write_cases

if TYPE_CHECKING:
    from pathlib import Path


def test_cases_round_trip_through_jsonl(tmp_path: Path) -> None:
    cases = [
        Case(
            id="s1-0000",
            input={"external_id": "s1-0000", "body": "Grüße"},
            expected={"outcome": "reply"},
            meta={"persona": "p-1"},
        )
    ]
    path = tmp_path / "out" / "cases.jsonl"
    write_cases(path, cases)
    assert "Grüße" in path.read_text(encoding="utf-8"), "UTF-8, not escaped"
    assert read_cases(path) == cases


def test_a_bad_line_is_reported_by_number_without_its_contents(tmp_path: Path) -> None:
    good = Case(id="a", input={}, expected={}, meta={}).model_dump_json()
    path = tmp_path / "cases.jsonl"
    path.write_text(good + '\n{"id": "b", "input": {"body": "SECRET-TEXT"}}\n')
    with pytest.raises(CaseFormatError, match="line 2") as exc:
        read_cases(path)
    assert "SECRET-TEXT" not in str(exc.value)
