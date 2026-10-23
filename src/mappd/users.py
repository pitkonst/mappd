"""The users table: operators who sign in when MAPPD_AUTH=jwt.

Passwords are salted scrypt (stdlib `hashlib.scrypt`), stored as
`scrypt$N$r$p$<salt>$<hash>` with both parts base64url-encoded. Roles are a JSONB
list; `admin` implies every other role (mappd.web.auth.Principal.has).

All functions take a `mappd.db.Connection` and run SQLAlchemy Core through
`mappd.db.run`; callers open the connection (`mappd.db.connect` or a transaction
step's). The CLI (`mappd user`) and the token/login routes share them
(AGENTS.md, rule 7).
"""

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

import sqlalchemy as sa
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from mappd import db
from mappd.db.tables import USERS

if TYPE_CHECKING:
    from collections.abc import Iterable

type Role = Literal["operator", "dispatcher", "admin"]
ROLES: tuple[Role, ...] = ("operator", "dispatcher", "admin")

# `auto` is the autonomy operator on decision records; `system` names the app itself.
RESERVED = ("auto", "system")

NAME_RE = re.compile(r"[a-z0-9._@-]{1,64}")
MIN_PASSWORD = 8
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


class UserExistsError(ValueError):
    """`add` was called for a name that is already a user."""


class User(BaseModel):
    """A user as callers see it; the password hash never leaves this module."""

    name: str
    roles: list[Role]
    disabled: bool
    created_at: datetime
    updated_at: datetime


def hash_password(password: str) -> str:
    """A `scrypt$N$r$p$<salt b64url>$<hash b64url>` string for `password`."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode()

    return f"scrypt$16384$8$1${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Whether `password` matches `stored`; False (never raises) on a malformed string."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(hash_b64)
        work = int(n)
        if work > 2**20:  # never honour a cost this high; the value is stored, not negotiated
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=salt, n=work, r=int(r), p=int(p), dklen=len(expected)
        )
    except ValueError, TypeError:
        return False
    return hmac.compare_digest(digest, expected)


_DUMMY_HASH = hash_password("not-a-real-password")


def _check_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"user name {name!r}: 1-64 characters of a-z, 0-9, '.', '_', '@', '-'")
    if name in RESERVED:
        raise ValueError(f"user name {name!r} is reserved")


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")


def _check_roles(roles: Iterable[str]) -> list[Role]:
    unknown = [r for r in roles if r not in ROLES]
    if unknown:
        raise ValueError(f"unknown role(s) {', '.join(unknown)}; roles are {', '.join(ROLES)}")
    return cast("list[Role]", list(dict.fromkeys(roles)))


def _user(row: dict[str, object]) -> User:
    roles = row["roles"] if isinstance(row["roles"], list) else []
    return User(
        name=str(row["name"]),
        roles=cast("list[Role]", [r for r in roles if r in ROLES]),
        disabled=bool(row["disabled"]),
        created_at=cast("datetime", row["created_at"]),
        updated_at=cast("datetime", row["updated_at"]),
    )


def _row(conn: db.Connection, name: str) -> dict[str, object] | None:
    found = db.run(conn, sa.select(USERS).where(USERS.c.name == name)).fetchone()
    return dict(found) if found else None


def add(
    conn: db.Connection, name: str, password: str, roles: Iterable[str] = ("operator",)
) -> User:
    """Insert a user; raises `UserExistsError` on a duplicate name, `ValueError` on bad input."""
    _check_name(name)
    _check_password(password)
    clean = _check_roles(roles)
    if _row(conn, name) is not None:
        raise UserExistsError(f"user {name!r} already exists")
    db.run(
        conn,
        sa.insert(USERS).values(
            name=name, password_hash=hash_password(password), roles=Jsonb(clean)
        ),
    )
    found = _row(conn, name)
    if found is None:
        raise LookupError(f"user {name!r} vanished after insert")  # pragma: no cover
    return _user(found)


def set_password(conn: db.Connection, name: str, password: str) -> None:
    """Replace `name`'s password; raises `LookupError` for an unknown user."""
    _check_password(password)
    cursor = db.run(
        conn,
        sa.update(USERS)
        .where(USERS.c.name == name)
        .values(password_hash=hash_password(password), updated_at=sa.text("now()")),
    )
    if cursor.rowcount == 0:
        raise LookupError(f"no user {name!r}")


def set_disabled(conn: db.Connection, name: str, disabled: bool) -> None:
    """Disable or re-enable `name`; raises `LookupError` for an unknown user."""
    cursor = db.run(
        conn,
        sa.update(USERS)
        .where(USERS.c.name == name)
        .values(disabled=disabled, updated_at=sa.text("now()")),
    )
    if cursor.rowcount == 0:
        raise LookupError(f"no user {name!r}")


def set_roles(conn: db.Connection, name: str, roles: Iterable[str]) -> None:
    """Replace `name`'s roles; raises `LookupError` for an unknown user."""
    cursor = db.run(
        conn,
        sa.update(USERS)
        .where(USERS.c.name == name)
        .values(roles=Jsonb(_check_roles(roles)), updated_at=sa.text("now()")),
    )
    if cursor.rowcount == 0:
        raise LookupError(f"no user {name!r}")


def get(conn: db.Connection, name: str) -> User | None:
    """The user named `name`, or None."""
    row = _row(conn, name)
    return _user(row) if row else None


def list_users(conn: db.Connection) -> list[User]:
    """Every user, by name."""
    rows = db.run(conn, sa.select(USERS).order_by(USERS.c.name)).fetchall()
    return [_user(dict(r)) for r in rows]


def authenticate(conn: db.Connection, name: str, password: str) -> User | None:
    """The user `name` when `password` matches and the user is enabled, else None.

    An unknown name still runs one scrypt verify against a dummy hash, so the
    timing does not reveal whether the user exists.
    """
    row = _row(conn, name)
    stored = str(row["password_hash"]) if row else _DUMMY_HASH
    ok = verify_password(password, stored)
    if row is None or not ok or row["disabled"]:
        return None
    return _user(row)


async def aget(conn: db.AsyncConnection, name: str) -> User | None:
    """Async `get`, for request-time lookups (db.aconnect / db.arun)."""
    found = await (await db.arun(conn, sa.select(USERS).where(USERS.c.name == name))).fetchone()
    return _user(dict(found)) if found else None


def count_admins(conn: db.Connection) -> int:
    """How many enabled users hold the admin role (used by `mappd doctor`)."""
    return sum(1 for u in list_users(conn) if not u.disabled and "admin" in u.roles)
