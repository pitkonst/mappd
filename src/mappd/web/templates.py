"""Jinja templates: the core layout, extended by each process's own templates/ folder."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, select_autoescape

if TYPE_CHECKING:
    from pathlib import Path


def utc(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M", *, label: bool = True) -> str:
    """`value` in UTC, labelled: "2026-10-23 14:03 UTC" ("" for None).

    A naive datetime is taken to be UTC already. `label=False` leaves the label off, for a
    date shown next to its labelled time.
    """
    if value is None:
        return ""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.strftime(fmt) + (" UTC" if label else "")


def brief(value: object) -> str:
    """A value in a few words for a person: "sku: CTL-H400 · on hand: 4", "a, b"."""
    if isinstance(value, dict):
        return " · ".join(f"{str(k).replace('_', ' ')}: {brief(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(brief(v) for v in value)
    return "" if value is None else str(value)


def readable_id(workflow_id: str) -> str:
    """A workflow ID for reading: `ids.from_key` escapes `:` and `%` inside a key, undone here.

    For display only; links and copies use the ID itself.
    """
    return workflow_id.replace("%3A", ":").replace("%25", "%")


def templates_for(*dirs: Path) -> Jinja2Templates:
    """Templates searched in `dirs` first, then the core (layout.html, error.html)."""
    loader = ChoiceLoader(
        [*(FileSystemLoader(d) for d in dirs), PackageLoader("mappd.web", "templates")]
    )
    env = Environment(loader=loader, autoescape=select_autoescape(default=True))
    env.filters["utc"] = utc
    env.filters["brief"] = brief
    env.filters["readable_id"] = readable_id
    return Jinja2Templates(env=env)


CORE = templates_for()
