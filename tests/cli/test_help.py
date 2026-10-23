"""Every leaf command's --help exits 0 and prints its docstring's example verbatim."""

import inspect
import re
from typing import TYPE_CHECKING

import typer.main
from typer.testing import CliRunner

from mappd.cli import app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from typer._click.core import Command

runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\]8;[^\x1b]*\x1b\\")


def _leaves(
    command: Command, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Command]]:
    subs = getattr(command, "commands", None)  # typer vendored click: no isinstance
    if subs:
        for name, sub in sorted(subs.items()):
            yield from _leaves(sub, (*path, name))
    else:
        yield path, command


def _example_lines(callback: object) -> list[str]:
    doc = inspect.getdoc(callback) or ""
    if "Example:" not in doc:
        return []
    return [
        line.strip()
        for line in doc.split("Example:", 1)[1].splitlines()
        if line.strip().startswith("mappd")
    ]


def test_every_command_shows_its_example_verbatim() -> None:
    leaves = list(_leaves(typer.main.get_command(app)))
    assert len(leaves) > 20
    for path, leaf in leaves:
        r = runner.invoke(app, [*path, "--help"], env={"COLUMNS": "200"})
        assert r.exit_code == 0, path
        out = ANSI.sub("", r.stdout)  # typer forces styling when GITHUB_ACTIONS is set
        assert "Example:" in out, f"mappd {' '.join(path)} --help has no example"
        for line in _example_lines(leaf.callback):
            assert line in out, f"mappd {' '.join(path)} --help lacks {line!r}"
