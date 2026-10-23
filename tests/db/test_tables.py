"""The declared table metadata must match what the migrations create."""

from typing import TYPE_CHECKING

import sqlalchemy as sa

from mappd.db import Connection, connect
from mappd.db.tables import ACTIONS, MAIL_CURSORS, USERS

if TYPE_CHECKING:
    from mappd.settings import Settings


def _columns(conn: Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    ).fetchall()
    return {r["column_name"] for r in rows}


def test_actions_columns_match_the_migration(migrated: Settings) -> None:
    with connect(migrated) as conn:
        assert set(ACTIONS.c.keys()) == _columns(conn, "actions")


def test_actions_indexes_match_the_migration(migrated: Settings) -> None:
    with connect(migrated) as conn:
        rows = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'actions'"
        ).fetchall()
    assert {i.name for i in ACTIONS.indexes} <= {r["indexname"] for r in rows}


def test_actions_key_constraints(migrated: Settings) -> None:
    assert ACTIONS.c.idempotency_key.unique
    assert ACTIONS.c.payload.nullable is False
    assert isinstance(ACTIONS.c.payload.type, sa.dialects.postgresql.JSONB)


def test_mail_cursor_columns_match_the_migration(migrated: Settings) -> None:
    with connect(migrated) as conn:
        assert set(MAIL_CURSORS.c.keys()) == _columns(conn, "mappd_mail_cursors")


def test_user_columns_match_the_migration(migrated: Settings) -> None:
    with connect(migrated) as conn:
        assert set(USERS.c.keys()) == _columns(conn, "mappd_users")
