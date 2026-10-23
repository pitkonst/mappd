from typing import TYPE_CHECKING

import pytest

from mappd.data.importer import RowError, import_rows, parse_map, read_rows
from processes.support.schemas import TicketIn

if TYPE_CHECKING:
    from pathlib import Path


def test_parse_map_defaults_to_input() -> None:
    assert parse_map("external_id=ticket_id,body=text,expected.route=queue") == {
        "input.external_id": "ticket_id",
        "input.body": "text",
        "expected.route": "queue",
    }


def test_parse_map_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="dst=src"):
        parse_map("body")


def test_csv_rows_become_cases(tmp_path: Path) -> None:
    path = tmp_path / "history.csv"
    path.write_text(
        "ticket_id,from,title,text,queue\nT-1,a@x.example,Hi,Sensor offline,technical\n"
    )
    mapping = parse_map(
        "external_id=ticket_id,sender=from,subject=title,body=text,expected.route=queue"
    )
    cases, errors = import_rows(read_rows(path), mapping, TicketIn, skip_invalid=False)
    assert errors == []
    [case] = cases
    assert case.id == "T-1"
    assert case.input["body"] == "Sensor offline"
    assert case.expected == {"route": "technical"}


def test_bad_row_is_reported_by_line(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text('{"id": "T-1", "text": "ok", "from": "a"}\n{"id": "T-2"}\n')
    mapping = parse_map("external_id=id,sender=from,subject=id,body=text")
    cases, errors = import_rows(read_rows(path), mapping, TicketIn, skip_invalid=True)
    assert [c.id for c in cases] == ["T-1"]
    assert [e.line for e in errors] == [2]
    assert isinstance(errors[0], RowError)


def test_malformed_json_line_is_a_row_error(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text('{"id": "T-1", "text": "ok", "from": "a"}\n{not json\n')
    mapping = parse_map("external_id=id,sender=from,subject=id,body=text")
    cases, errors = import_rows(read_rows(path), mapping, TicketIn, skip_invalid=True)
    assert [c.id for c in cases] == ["T-1"]
    assert [e.line for e in errors] == [2]
    with pytest.raises(RowError, match="line 2"):
        import_rows(read_rows(path), mapping, TicketIn, skip_invalid=False)


def test_a_mapped_column_missing_from_the_csv_header_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "history.csv"
    path.write_text("id,from,title,text\nT-1,a@x.example,Hi,Sensor offline\n")
    mapping = parse_map("external_id=id,sender=from,subject=title,body=text,expected.route=queue")
    with pytest.raises(ValueError, match="queue"):
        import_rows(read_rows(path), mapping, TicketIn, skip_invalid=False)
