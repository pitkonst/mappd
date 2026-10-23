"""Real history into the case format: CSV or JSONL plus a field map onto the process input."""

import csv
import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from mappd.data.cases import Case

if TYPE_CHECKING:
    from pathlib import Path


class RowError(ValueError):
    """A row that does not fit the process input."""

    def __init__(self, line: int, message: str) -> None:
        """Remember the line."""
        super().__init__(f"line {line}: {message}")
        self.line = line


def parse_map(text: str) -> dict[str, str]:
    """`dst=src,...`; a bare destination means `input.<dst>`."""
    mapping: dict[str, str] = {}
    for part in text.split(","):
        dst, sep, src = part.strip().partition("=")
        if not sep or not dst or not src:
            raise ValueError(f"expected dst=src, got {part!r}")
        mapping[dst if dst.startswith(("input.", "expected.")) else f"input.{dst}"] = src
    return mapping


type Row = dict[str, Any] | RowError


def read_rows(path: Path) -> list[tuple[int, Row]]:
    """Rows with their line numbers (CSV with a header, or JSON lines); bad lines are RowErrors."""
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            return [(n, dict(row)) for n, row in enumerate(csv.DictReader(f), start=2)]
    rows: list[tuple[int, Row]] = []
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as e:
                rows.append((n, RowError(n, f"not JSON: {e.msg}")))
                continue
            rows.append((n, value if isinstance(value, dict) else RowError(n, "not a JSON object")))
    return rows


def import_rows(
    rows: list[tuple[int, Row]],
    mapping: dict[str, str],
    input_model: type[BaseModel],
    *,
    skip_invalid: bool,
) -> tuple[list[Case], list[RowError]]:
    """Cases from rows; invalid rows become errors (raised unless `skip_invalid`)."""
    seen = {key for _, row in rows if isinstance(row, dict) for key in row}
    unknown = sorted(src for src in mapping.values() if src not in seen)
    if rows and unknown:
        raise ValueError(f"--map names columns no row has: {', '.join(unknown)}")
    cases: list[Case] = []
    errors: list[RowError] = []
    for line, row in rows:
        if isinstance(row, RowError):
            if not skip_invalid:
                raise row
            errors.append(row)
            continue
        values: dict[str, dict[str, Any]] = {"input": {}, "expected": {}}
        for dst, src in mapping.items():
            section, field = dst.split(".", 1)
            if src in row:
                values[section][field] = row[src]
        try:
            ticket = input_model.model_validate(values["input"])
        except ValidationError as e:
            error = RowError(line, str(e.errors()[0]["msg"]) + f" ({e.errors()[0]['loc']})")
            if not skip_invalid:
                raise error from None
            errors.append(error)
            continue
        data = ticket.model_dump()
        case_id = str(data.get("external_id") or f"row-{line}")
        cases.append(
            Case(id=case_id, input=data, expected=values["expected"], meta={"imported": line})
        )
    return cases, errors
