"""mappd doctor: checks grouped by MAPP-D layer."""

import os
from dataclasses import asdict
from typing import Annotated

import typer

from mappd import doctor as checks
from mappd.cli._common import EXIT_FAILED, JsonFlag, emit, load_settings

_LABEL = {"ok": "ok", "off": "off", "skip": "skip", "fail": "FAIL"}


def _style(text: str, color: bool, **kwargs: object) -> str:
    """Style already-padded `text`; plain when colour is off or NO_COLOR is set."""
    if not color or "NO_COLOR" in os.environ:
        return text
    return typer.style(text, **kwargs)  # type: ignore[arg-type]


def _lines(results: list[checks.Check], color: bool) -> list[str]:
    """Groups in GROUPS order; one row per check, a fix line under each failure."""
    name_width = max(len(c.name) for c in results)
    detail_width = max(len(c.detail) for c in results)
    lines: list[str] = []
    counts = {"ok": 0, "off": 0, "skip": 0, "fail": 0}
    for group, (layer, title) in checks.GROUPS.items():
        checks_in_group = [c for c in results if c.group == group]
        if not checks_in_group:
            continue
        if lines:
            lines.append("")
        lines.append(_style(f"{layer}  {title}", color, bold=True))
        for c in checks_in_group:
            counts[c.status] += 1
            status = _LABEL[c.status].ljust(4)
            styled = {
                "ok": _style(status, color, fg="green"),
                "fail": _style(status, color, fg="red", bold=True),
            }.get(c.status, _style(status, color, dim=True))
            row = f"   {styled}  {c.name.ljust(name_width)}  {c.detail.ljust(detail_width)}"
            if c.target is not None:
                row += f"  {_style(c.target, color, dim=True)}"
            lines.append(row.rstrip())
            if c.status == "fail" and c.fix is not None:
                fix = _style("fix:", color, fg="yellow")
                lines.append(f"{'':{3 + 4 + 2}}{fix} {c.fix}")
    parts = [f"{counts['ok']} ok"]
    if counts["off"]:
        parts.append(f"{counts['off']} off")
    if counts["skip"]:
        parts.append(f"{counts['skip']} skipped")
    failed = f"{counts['fail']} failed"
    parts.append(_style(failed, color, fg="red") if counts["fail"] else failed)
    lines += ["", ", ".join(parts)]
    return lines


def doctor(
    as_json: JsonFlag = False,
    health: Annotated[
        bool,
        typer.Option(
            "--health",
            help="The container healthcheck: no mail-server logins, no workflows check.",
        ),
    ] = False,
) -> None:
    """Check every MAPP-D layer; also the container healthcheck.

    Checks are grouped by layer: M model gateway, A agents, P Postgres, P processes,
    · traces, D workflows. Each is ok, off (not configured), skip (left out by --health)
    or FAIL; a failing check prints the command that fixes it.

    Exit 0 when every check passes, 1 when one fails, 2 when the settings are invalid.

    Example:
        mappd doctor --json
        mappd doctor --health     # what the app container's healthcheck runs
    """
    settings = load_settings()
    results = checks.run_all(settings, health=health)
    emit(
        {
            "ok": all(c.ok for c in results),
            "summary": {
                key: sum(1 for c in results if c.status == key)
                for key in ("ok", "off", "skip", "fail")
            },
            "checks": [{**asdict(c), "ok": c.ok} for c in results],
        },
        as_json,
        _lines(results, color=True),
    )
    if any(c.status == "fail" for c in results):
        raise typer.Exit(EXIT_FAILED)
