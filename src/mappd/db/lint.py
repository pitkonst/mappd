"""Expand-only migration linter.

During a deploy the old version keeps running against the new schema, so drops and renames
wait for a later deploy. A migration that contains one must say so with a module-level
`contract = True`; everything else in `upgrade()` must only add.

Checked: `op.drop_*`, `op.rename_table`, `op.alter_column(new_column_name=...)`, and string
literals passed to `op.execute` (directly or in `text()`) containing DROP (except DROP NOT
NULL, DEFAULT, EXPRESSION, IDENTITY) or RENAME. SQL built at runtime is not seen.
"""

import ast
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mappd.db.migrate import version_locations

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from mappd.settings import Settings

DESTRUCTIVE_OPS = frozenset(
    {"drop_table", "drop_column", "drop_index", "drop_constraint", "rename_table"}
)
# Any DROP except the column-level ones that only relax a constraint (expand-safe).
DESTRUCTIVE_SQL = re.compile(
    r"\bDROP\s+(?!NOT\s+NULL\b|DEFAULT\b|EXPRESSION\b|IDENTITY\b)\w+|\bRENAME\b",
    re.IGNORECASE,
)
HINT = "move it to a later migration that sets `contract = True`"


@dataclass(frozen=True)
class Violation:
    """A destructive operation in an expand migration."""

    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def migration_files(settings: Settings) -> list[Path]:
    """Every migration module of the core and the processes."""
    return [
        path
        for location in version_locations(settings)
        for path in sorted(location.glob("*.py"))
        if path.name != "__init__.py"
    ]


def check(paths: Iterable[Path]) -> list[Violation]:
    """Lint every file in `paths`, in order."""
    return [violation for path in paths for violation in check_file(path)]


def check_file(path: Path) -> list[Violation]:
    """Lint one migration module."""
    tree = ast.parse(path.read_text(), filename=str(path))
    if _is_contract(tree):
        return []
    upgrade = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None
    )
    if upgrade is None:
        return []
    return [
        Violation(path, node.lineno, message)
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Call) and (message := _destructive(node))
    ]


def _is_contract(tree: ast.Module) -> bool:
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        value = getattr(node, "value", None)
        if (
            any(isinstance(t, ast.Name) and t.id == "contract" for t in targets)
            and isinstance(value, ast.Constant)
            and value.value is True
        ):
            return True
    return False


def _destructive(call: ast.Call) -> str | None:
    func = call.func
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
        return None
    if func.value.id != "op":
        return None
    if func.attr in DESTRUCTIVE_OPS:
        return f"op.{func.attr} in an expand migration; {HINT}"
    if func.attr == "alter_column" and any(k.arg == "new_column_name" for k in call.keywords):
        return f"column rename in an expand migration; {HINT}"
    if func.attr == "execute" and call.args:
        sql = _literal_sql(call.args[0])
        if sql is not None and DESTRUCTIVE_SQL.search(sql):
            return f"DROP or RENAME in op.execute in an expand migration; {HINT}"
    return None


def _literal_sql(node: ast.expr) -> str | None:
    """The SQL string in `"..."`, `text("...")` or `sa.text("...")`; None if built at runtime."""
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == "text":
            node = node.args[0]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None
