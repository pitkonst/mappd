from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from mappd.db import Connection, connect
from mappd.locking import update_if_version

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mappd.settings import Settings


@pytest.fixture
def conn(migrated: Settings) -> Iterator[Connection]:
    with connect(migrated) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS lock_demo ("
            " id bigserial PRIMARY KEY, status text NOT NULL, note text,"
            " version int NOT NULL DEFAULT 0)"
        )
        yield c


def insert(conn: Connection, status: str = "awaiting_approval") -> int:
    row = conn.execute(
        "INSERT INTO lock_demo (status) VALUES (%s) RETURNING id", (status,)
    ).fetchone()
    assert row is not None
    return int(row["id"])


def read(conn: Connection, row_id: int) -> dict[str, object]:
    row = conn.execute(
        "SELECT status, note, version FROM lock_demo WHERE id=%s", (row_id,)
    ).fetchone()
    assert row is not None
    return dict(row)


def test_matching_version_applies_changes_and_bumps(conn: Connection) -> None:
    rid = insert(conn)
    assert update_if_version(conn, "lock_demo", rid, 0, {"status": "approved", "note": "ok"})
    assert read(conn, rid) == {"status": "approved", "note": "ok", "version": 1}


def test_stale_version_changes_nothing(conn: Connection) -> None:
    rid = insert(conn)
    assert update_if_version(conn, "lock_demo", rid, 0, {"note": "first"})
    assert not update_if_version(conn, "lock_demo", rid, 0, {"note": "stale"})
    assert read(conn, rid) == {"status": "awaiting_approval", "note": "first", "version": 1}


def test_where_must_match_too(conn: Connection) -> None:
    rid = insert(conn, status="sent")
    assert not update_if_version(
        conn, "lock_demo", rid, 0, {"status": "approved"}, where={"status": "awaiting_approval"}
    )
    assert read(conn, rid)["version"] == 0


def test_missing_row_is_false(conn: Connection) -> None:
    assert not update_if_version(conn, "lock_demo", -1, 0, {"note": "x"})


def test_concurrent_decisions_on_one_version_have_one_winner(
    migrated: Settings, conn: Connection
) -> None:
    rid = insert(conn)

    def decide(n: int) -> bool:
        with connect(migrated) as c:
            return update_if_version(c, "lock_demo", rid, 0, {"note": f"operator {n}"})

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(decide, range(4)))
    assert results.count(True) == 1
    assert read(conn, rid)["version"] == 1


def test_version_cannot_be_set_directly(conn: Connection) -> None:
    with pytest.raises(ValueError, match="version"):
        update_if_version(conn, "lock_demo", 1, 0, {"version": 5})


@pytest.mark.parametrize(
    ("table", "changes"),
    [("lock_demo; DROP TABLE x", {"note": "x"}), ("lock_demo", {"Note": "x"}), ("lock_demo", {})],
)
def test_names_and_changes_are_validated(
    conn: Connection, table: str, changes: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match=r"identifier|changes"):
        update_if_version(conn, table, 1, 0, changes)
