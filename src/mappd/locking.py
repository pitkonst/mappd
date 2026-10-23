"""Optimistic locking: a decision applies only to the version the person saw.

Every table holding a case has an integer `version` column. A decision names the version it
was made on; if the row moved on, the update matches nothing and the API answers 409.
"""

import re
from typing import TYPE_CHECKING

import sqlalchemy as sa

from mappd.db import run

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mappd.db import Connection

IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def update_if_version(
    conn: Connection,
    table: str,
    row_id: object,
    version: int,
    changes: Mapping[str, object],
    *,
    where: Mapping[str, object] | None = None,
) -> bool:
    """Apply `changes` to row `row_id` if it is still at `version`, and bump the version.

    `where` adds equality conditions (for example the status the decision expects).

    Returns:
        False when the row is missing, at another version, or does not match `where`.
    """
    conditions = dict(where or {})
    if not changes:
        raise ValueError("changes must not be empty")
    if "version" in changes:
        raise ValueError("version is bumped automatically; do not set it in changes")
    for name in (table, *changes, *conditions):
        if not IDENTIFIER.fullmatch(name):
            raise ValueError(f"not a lowercase SQL identifier: {name!r}")

    row = sa.table(
        table,
        sa.column("id"),
        sa.column("version"),
        *(sa.column(k) for k in (*changes, *conditions)),
    )
    stmt = (
        row.update()
        .where(
            row.c.id == sa.bindparam("row_id"),
            row.c.version == sa.bindparam("version"),
            *(row.c[k] == sa.bindparam(f"where_{k}") for k in conditions),
        )
        .values(version=row.c.version + 1, **{k: sa.bindparam(f"set_{k}") for k in changes})
        .returning(row.c.id)
    )
    params: dict[str, object] = {"row_id": row_id, "version": version}
    params.update({f"set_{k}": v for k, v in changes.items()})
    params.update({f"where_{k}": v for k, v in conditions.items()})
    return run(conn, stmt, params).fetchone() is not None
