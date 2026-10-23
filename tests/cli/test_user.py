"""mappd user: account management for MAPPD_AUTH=jwt (pg)."""

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from mappd.cli import app
from mappd.db import connect
from mappd.users import authenticate, get

if TYPE_CHECKING:
    from mappd.settings import Settings

runner = CliRunner()


@pytest.fixture
def env(migrated: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MAPPD_DATABASE_URL", migrated.database_url)
    with connect(migrated) as conn:
        conn.execute("DELETE FROM mappd_users WHERE name LIKE 'cli-%'")
    return migrated


def test_add_list_and_authenticate(env: Settings) -> None:
    r = runner.invoke(
        app,
        ["user", "add", "cli-anna", "--role", "operator", "--role", "admin", "--password-stdin"],
        input="s3cret-pass\n",
    )
    assert r.exit_code == 0, r.stderr
    listing = runner.invoke(app, ["user", "list", "--json"])
    rows = {u["name"]: u for u in json.loads(listing.stdout)["users"]}
    assert rows["cli-anna"]["roles"] == ["operator", "admin"]
    assert rows["cli-anna"]["disabled"] is False
    with connect(env) as conn:
        assert authenticate(conn, "cli-anna", "s3cret-pass") is not None


def test_add_defaults_to_operator(env: Settings) -> None:
    r = runner.invoke(app, ["user", "add", "cli-ben", "--password-stdin"], input="s3cret-pw\n")
    assert r.exit_code == 0, r.stderr
    with connect(env) as conn:
        assert get(conn, "cli-ben") is not None
        assert get(conn, "cli-ben").roles == ["operator"]  # type: ignore[union-attr]


def test_the_password_never_appears_in_output(env: Settings) -> None:
    r = runner.invoke(app, ["user", "add", "cli-cat", "--password-stdin"], input="t0p-secret\n")
    assert "t0p-secret" not in r.stdout + r.stderr
    listing = runner.invoke(app, ["user", "list"])
    assert "t0p-secret" not in listing.stdout


def test_without_password_stdin_is_a_usage_error(env: Settings) -> None:
    assert runner.invoke(app, ["user", "add", "cli-dan"]).exit_code == 2


@pytest.mark.parametrize(
    ("args", "stdin"),
    [
        (["user", "add", "cli-anna", "--password-stdin"], "first-pw\n"),  # duplicate
        (["user", "add", "Bad Name", "--password-stdin"], "s3cret\n"),
        (["user", "add", "auto", "--password-stdin"], "s3cret\n"),
        (["user", "add", "cli-short", "--password-stdin"], "short\n"),
        (["user", "add", "cli-role", "--role", "superuser", "--password-stdin"], "s3cret\n"),
        (["user", "passwd", "cli-ghost", "--password-stdin"], "s3cret-pw\n"),
        (["user", "roles", "cli-ghost", "--role", "admin"], None),
        (["user", "roles", "cli-anna"], None),  # no --role
        (["user", "disable", "cli-ghost"], None),
        (["user", "enable", "cli-ghost"], None),
    ],
)
def test_errors_exit_2(env: Settings, args: list[str], stdin: str | None) -> None:
    runner.invoke(app, ["user", "add", "cli-anna", "--password-stdin"], input="first-pw\n")
    r = runner.invoke(app, args, input=stdin)
    assert r.exit_code == 2, (args, r.stdout, r.stderr)


def test_passwd_roles_disable_enable(env: Settings) -> None:
    runner.invoke(app, ["user", "add", "cli-eve", "--password-stdin"], input="old-pass\n")
    r = runner.invoke(app, ["user", "passwd", "cli-eve", "--password-stdin"], input="new-pass\n")
    assert r.exit_code == 0, r.stderr
    with connect(env) as conn:
        assert authenticate(conn, "cli-eve", "new-pass") is not None
        assert authenticate(conn, "cli-eve", "old-pass") is None
    r = runner.invoke(app, ["user", "roles", "cli-eve", "--role", "dispatcher"])
    assert r.exit_code == 0, r.stderr
    with connect(env) as conn:
        assert get(conn, "cli-eve").roles == ["dispatcher"]  # type: ignore[union-attr]
    r = runner.invoke(app, ["user", "disable", "cli-eve"])
    assert r.exit_code == 0, r.stderr
    with connect(env) as conn:
        assert authenticate(conn, "cli-eve", "new-pass") is None
    r = runner.invoke(app, ["user", "enable", "cli-eve", "--json"])
    assert json.loads(r.stdout) == {"name": "cli-eve", "disabled": False}


def test_list_table(env: Settings) -> None:
    runner.invoke(app, ["user", "add", "cli-flo", "--password-stdin"], input="s3cret-pw\n")
    r = runner.invoke(app, ["user", "list"])
    assert r.exit_code == 0
    assert "cli-flo" in r.stdout
    assert "enabled" in r.stdout
