"""mappd.users: the sign-in table behind MAPPD_AUTH=jwt."""

import re
from typing import TYPE_CHECKING

import pytest

from mappd import users
from mappd.db import Connection, connect
from mappd.users import User, UserExistsError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from mappd.settings import Settings


@pytest.fixture
def conn(migrated: Settings) -> Iterator[Connection]:
    with connect(migrated) as c:
        c.execute("DELETE FROM mappd_users WHERE name LIKE 'test-%'")
        yield c


def test_hash_and_verify_roundtrip() -> None:
    stored = users.hash_password("correct-horse")
    assert re.fullmatch(r"scrypt\$16384\$8\$1\$[A-Za-z0-9_-]+={0,2}\$[A-Za-z0-9_-]+={0,2}", stored)
    assert users.verify_password("correct-horse", stored)
    assert not users.verify_password("wrong-horse", stored)
    assert users.hash_password("correct-horse") != stored, "a fresh salt each time"


@pytest.mark.parametrize(
    "stored",
    ["", "not-a-hash", "scrypt$1$2$3", "argon2$1$2$3$xx$yy", "scrypt$x$8$1$aa$bb"],
)
def test_verify_survives_malformed_stored_strings(stored: str) -> None:
    assert users.verify_password("whatever", stored) is False


def test_a_stored_string_with_an_absurd_cost_is_refused() -> None:
    assert not users.verify_password("pw", f"scrypt${2**60}$8$1$AAAA$BBBB")


@pytest.mark.parametrize("name", ["Auto", "auto", "system", "UPPER", "a b", "x" * 65, ""])
def test_bad_names_are_rejected(conn: Connection, name: str) -> None:
    with pytest.raises(ValueError, match="name"):
        users.add(conn, name, "long-enough")


def test_short_passwords_are_rejected(conn: Connection) -> None:
    with pytest.raises(ValueError, match="8"):
        users.add(conn, "test-short", "short")
    with pytest.raises(ValueError, match="8"):
        users.set_password(conn, "test-short", "short")


def test_add_get_and_authenticate(conn: Connection) -> None:
    users.add(conn, "test-anna", "secret-123", ("operator", "admin"))
    user = users.get(conn, "test-anna")
    assert user is not None
    assert (user.roles, user.disabled) == (["operator", "admin"], False)
    assert "password" not in user.model_dump()
    assert "hash" not in user.model_dump()
    assert users.authenticate(conn, "test-anna", "secret-123") == user
    assert users.authenticate(conn, "test-anna", "wrong-pass") is None


def test_duplicate_names_are_named_errors(conn: Connection) -> None:
    users.add(conn, "test-bob", "secret-123")
    with pytest.raises(UserExistsError, match="test-bob"):
        users.add(conn, "test-bob", "other-pass")


def test_unknown_roles_are_rejected(conn: Connection) -> None:
    with pytest.raises(ValueError, match="superuser"):
        users.add(conn, "test-cid", "secret-123", ("superuser",))
    users.add(conn, "test-cid", "secret-123")
    with pytest.raises(ValueError, match="superuser"):
        users.set_roles(conn, "test-cid", ("superuser",))


def test_updates_and_listing(conn: Connection) -> None:
    users.add(conn, "test-dan", "secret-123")
    users.set_password(conn, "test-dan", "new-secret-1")
    assert users.authenticate(conn, "test-dan", "new-secret-1") is not None
    assert users.authenticate(conn, "test-dan", "secret-123") is None
    users.set_roles(conn, "test-dan", ("dispatcher",))
    assert users.get(conn, "test-dan") is not None
    assert users.get(conn, "test-dan").roles == ["dispatcher"]  # type: ignore[union-attr]
    users.set_disabled(conn, "test-dan", True)
    assert users.authenticate(conn, "test-dan", "new-secret-1") is None, "disabled cannot sign in"
    users.set_disabled(conn, "test-dan", False)
    assert users.authenticate(conn, "test-dan", "new-secret-1") is not None
    names = [u.name for u in users.list_users(conn)]
    assert "test-dan" in names
    assert names == sorted(names)


def test_changes_to_unknown_users_are_lookup_errors(conn: Connection) -> None:
    calls: list[Callable[[], None]] = [
        lambda: users.set_password(conn, "test-ghost", "long-enough"),
        lambda: users.set_disabled(conn, "test-ghost", True),
        lambda: users.set_roles(conn, "test-ghost", ("admin",)),
    ]
    for call in calls:
        with pytest.raises(LookupError, match="test-ghost"):
            call()


def test_authenticate_unknown_name_returns_none_and_takes_time(conn: Connection) -> None:
    assert users.authenticate(conn, "test-nobody", "secret-123") is None
    assert users.count_admins(conn) == sum(
        1 for u in users.list_users(conn) if not u.disabled and "admin" in u.roles
    )


def test_aget_matches_get(migrated: Settings, conn: Connection) -> None:
    import asyncio  # noqa: PLC0415

    from mappd import db  # noqa: PLC0415

    async def fetch(name: str) -> User | None:
        async with db.aconnect(migrated) as aconn:
            return await users.aget(aconn, name)

    users.add(conn, "test-eve", "secret-123", ("dispatcher",))
    found = asyncio.run(fetch("test-eve"))
    assert found is not None
    assert (found.name, found.roles) == ("test-eve", ["dispatcher"])
    assert asyncio.run(fetch("test-nobody")) is None
