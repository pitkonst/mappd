"""Autonomy policy: shadow mode and per-category levels, in a file reviewed like code.

`processes/<name>/autonomy.yaml` says whether the process sends anything (`shadow`) and which
categories go out without approval (`auto`). The CLI edits the file line by line — comments
and order stay — and prints the diff to commit; the app reads it at startup, so a change ships
with a deploy. Hard rules in a process's domain code
override every level.
"""

import difflib
import re
import types
import typing
from typing import TYPE_CHECKING, Literal, get_args, get_origin

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

type Level = Literal["auto", "review"]
LEVELS: tuple[str, ...] = ("auto", "review")


class Readiness(BaseModel):
    """When the dashboard calls a category ready for `auto`."""

    model_config = ConfigDict(extra="forbid")  # a misspelled key must not pass silently

    without_edits: float = 0.9
    false_escalations_max: float = 0.1
    min_cases: int = 30


class Policy(BaseModel):
    """A process's autonomy policy."""

    model_config = ConfigDict(extra="forbid")  # `shadwo: true` must not leave shadow off

    shadow: bool = False
    default: Level = "review"
    categories: dict[str, Level] = {}
    ready: Readiness = Readiness()

    def level(self, category: str) -> Level:
        """The category's level, or the default when it is not listed."""
        return self.categories.get(category, self.default)


class PolicyError(ValueError):
    """The policy file cannot be read; the message names the file and the field."""


def load(path: Path) -> Policy:
    """The policy in `path`; a missing file is the safe default (review all, shadow off).

    Raises:
        PolicyError: malformed YAML or an invalid value.
    """
    if not path.is_file():
        return Policy()
    try:
        data = yaml.safe_load(path.read_text()) or {}
        return Policy.model_validate(data)
    except yaml.YAMLError as e:
        raise PolicyError(f"{path}: not YAML: {e}") from e
    except ValidationError as e:
        where = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        raise PolicyError(f"{path}: {where}") from e


_SHADOW = re.compile(r"^(shadow:\s*)(\S+)(.*)$")
_SECTION = re.compile(r"^categories:\s*(#.*)?$")


def _parse(text: str) -> Policy:
    try:
        return Policy.model_validate(yaml.safe_load(text) or {})
    except (yaml.YAMLError, ValidationError) as e:
        raise PolicyError(f"the edit would not load: {e}") from e


def _write(path: Path, before: str, after: str, expected: Policy) -> str:
    """Write `after` if it loads as `expected`; returns the diff ("" when nothing changes).

    Raises:
        PolicyError: the edited text would not load, or would not mean what was asked; the
            file is left as it was.
    """
    if before == after:
        return ""
    if _parse(after) != expected:
        raise PolicyError(f"{path}: the edit would not set what was asked; edit it by hand")
    path.write_text(after)
    lines = difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True), str(path), str(path)
    )
    return "".join(lines)


def _read(path: Path) -> tuple[str, list[str]]:
    """The file's text, and its lines with a final newline guaranteed."""
    before = path.read_text() if path.is_file() else ""
    text = before if not before or before.endswith("\n") else before + "\n"
    return before, text.splitlines(keepends=True)


def set_shadow(path: Path, on: bool) -> str:
    """Set `shadow:`; returns the unified diff ("" when it already had that value).

    Raises:
        PolicyError: the file does not load, or the edit would not.
    """
    before, lines = _read(path)
    current = _parse(before)
    if current.shadow is on:
        return ""
    expected = current.model_copy(update={"shadow": on})
    value = "true" if on else "false"
    for i, line in enumerate(lines):
        if m := _SHADOW.match(line.rstrip("\n")):
            lines[i] = f"{m.group(1)}{value}{m.group(3)}\n"
            return _write(path, before, "".join(lines), expected)
    return _write(path, before, f"shadow: {value}\n" + "".join(lines), expected)


_FLOW = re.compile(r"^categories:\s*\{\s*\}\s*(#.*)?$")


def set_level(path: Path, category: str, level: Level, *, allowed: Collection[str]) -> str:
    """Set one category's level under `categories:`; returns the diff ("" when unchanged).

    Raises:
        ValueError: `category` is not one of `allowed` or `level` is not auto/review.
        PolicyError: the file does not load, or the edit would not mean what was asked.
    """
    if category not in allowed:
        raise ValueError(f"unknown category {category!r}; one of {', '.join(allowed)}")
    if level not in LEVELS:
        raise ValueError(f"level must be auto or review, not {level!r}")
    before, lines = _read(path)
    current = _parse(before)
    if current.categories.get(category) == level:
        return ""
    expected = current.model_copy(update={"categories": {**current.categories, category: level}})
    entry = re.compile(rf"^(\s+{re.escape(category)}:\s*)(\S+)(.*)$")
    for i, line in enumerate(lines):
        if _FLOW.match(line.rstrip("\n")):  # `categories: {}` becomes a block with one entry
            lines[i] = f"categories:\n  {category}: {level}\n"
            return _write(path, before, "".join(lines), expected)
    section = next((i for i, line in enumerate(lines) if _SECTION.match(line.rstrip("\n"))), None)
    if section is None:
        return _write(
            path, before, "".join(lines) + f"categories:\n  {category}: {level}\n", expected
        )
    indent = "  "
    i = section + 1
    while i < len(lines) and (
        lines[i].startswith((" ", "\t", "#")) or not lines[i].strip()
    ):  # the section: indented entries, blank lines, comments
        line = lines[i].rstrip("\n")
        if line.strip() and not line.lstrip().startswith("#"):
            indent = line[: len(line) - len(line.lstrip())]
            if m := entry.match(line):
                lines[i] = f"{m.group(1)}{level}{m.group(3)}\n"
                return _write(path, before, "".join(lines), expected)
        i += 1
    lines.insert(section + 1, f"{indent}{category}: {level}\n")
    return _write(path, before, "".join(lines), expected)


def categories(model: type[BaseModel], field: str) -> tuple[str, ...]:
    """The allowed values of a `Literal` field (through `type` aliases and `| None`)."""
    tp: object = model.model_fields[field].annotation
    while True:
        if isinstance(tp, typing.TypeAliasType):
            tp = tp.__value__
        elif get_origin(tp) in (typing.Union, types.UnionType):
            tp = next(a for a in get_args(tp) if a is not type(None))
        else:
            break
    if get_origin(tp) is not Literal:
        raise TypeError(f"{model.__name__}.{field} is not a Literal")
    return tuple(str(a) for a in get_args(tp))
