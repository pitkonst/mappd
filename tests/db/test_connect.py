from typing import TYPE_CHECKING

from mappd.db import connect

if TYPE_CHECKING:
    from mappd.settings import Settings


def test_connect_returns_dict_rows_in_autocommit(pg_settings: Settings) -> None:
    with connect(pg_settings) as conn:
        assert conn.autocommit
        assert conn.execute("SELECT 1 AS one").fetchone() == {"one": 1}
