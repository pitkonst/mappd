"""mappd user: the operators who sign in when MAPPD_AUTH=jwt."""

import sys
from collections.abc import Callable
from typing import Annotated

import typer

from mappd import users
from mappd.cli._common import (
    DB_ERRORS,
    EXIT_USAGE,
    JsonFlag,
    emit,
    fail,
    load_settings,
    table,
)
from mappd.db import Connection, connect

app = typer.Typer(help="P: user accounts for sign-in.", no_args_is_help=True)

RoleOption = Annotated[
    list[str] | None,
    typer.Option("--role", help="A role: operator, dispatcher, admin. Repeatable."),
]
PasswordStdin = Annotated[
    bool, typer.Option("--password-stdin", help="Read the password from stdin (required).")
]


def _password(flag: bool) -> str:
    """The password from stdin; one trailing newline is the line ending, not the password."""
    if not flag:
        fail("pass --password-stdin: passwords are only read from stdin", EXIT_USAGE)
    raw = sys.stdin.read()
    return raw[:-1] if raw.endswith("\n") else raw


def _run[T](work: Callable[[Connection], T]) -> T:
    """Open the app database and run `work`; usage errors exit 2, outages exit 1."""
    settings = load_settings()
    try:
        with connect(settings) as conn:
            return work(conn)
    except (ValueError, LookupError) as e:
        fail(str(e), EXIT_USAGE)
    except DB_ERRORS as e:
        fail(f"app database unavailable: {e}")
    raise AssertionError("unreachable")  # pragma: no cover


@app.command()
def add(
    name: Annotated[str, typer.Argument(help="1-64 chars of a-z 0-9 . _ @ -")],
    roles: RoleOption = None,
    password_stdin: PasswordStdin = False,
    as_json: JsonFlag = False,
) -> None:
    """Create a user.

    Example:
        echo 's3cret-pass' | mappd user add alice --role operator --password-stdin
    """
    password = _password(password_stdin)
    user = _run(lambda c: users.add(c, name, password, roles or ["operator"]))
    emit(
        user.model_dump(mode="json"),
        as_json,
        [f"{user.name}: added ({', '.join(user.roles) or 'no roles'})"],
    )


@app.command("list")
def list_(as_json: JsonFlag = False) -> None:
    """Every user.

    Example:
        mappd user list --json
    """
    rows = _run(users.list_users)
    emit(
        {"users": [u.model_dump(mode="json") for u in rows]},
        as_json,
        table(
            ["NAME", "ROLES", "STATUS", "CREATED"],
            [
                [
                    u.name,
                    ",".join(u.roles),
                    "disabled" if u.disabled else "enabled",
                    str(u.created_at.date()),
                ]
                for u in rows
            ],
        )
        or ["no users"],
    )


@app.command()
def passwd(
    name: Annotated[str, typer.Argument(help="The user whose password to replace.")],
    password_stdin: PasswordStdin = False,
    as_json: JsonFlag = False,
) -> None:
    """Set a new password; existing tokens stay valid until they expire.

    Example:
        echo 'n3w-pass' | mappd user passwd alice --password-stdin
    """
    password = _password(password_stdin)
    _run(lambda c: users.set_password(c, name, password))
    emit({"name": name, "password": "changed"}, as_json, [f"{name}: password changed"])


@app.command()
def roles(
    name: Annotated[str, typer.Argument(help="The user whose roles to replace.")],
    roles_: RoleOption = None,
    as_json: JsonFlag = False,
) -> None:
    """Replace a user's roles (pass each one with --role).

    Example:
        mappd user roles alice --role operator --role admin
    """
    if not roles_:
        fail("pass at least one --role (the set replaces the current roles)", EXIT_USAGE)
    _run(lambda c: users.set_roles(c, name, roles_))
    emit({"name": name, "roles": roles_}, as_json, [f"{name}: roles {', '.join(roles_)}"])


def _set_disabled(name: str, disabled: bool, as_json: bool) -> None:
    _run(lambda c: users.set_disabled(c, name, disabled))
    state = "disabled" if disabled else "enabled"
    emit({"name": name, "disabled": disabled}, as_json, [f"{name}: {state}"])


@app.command()
def disable(
    name: Annotated[str, typer.Argument(help="The user to lock out.")],
    as_json: JsonFlag = False,
) -> None:
    """Lock a user out; their tokens stop working on the next request.

    Example:
        mappd user disable alice
    """
    _set_disabled(name, True, as_json)


@app.command()
def enable(
    name: Annotated[str, typer.Argument(help="The user to let back in.")],
    as_json: JsonFlag = False,
) -> None:
    """Re-enable a disabled user.

    Example:
        mappd user enable alice
    """
    _set_disabled(name, False, as_json)
