import json
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from typer.testing import CliRunner

from mappd import actions
from mappd.cli import app
from mappd.cli.action import ActionStatus

if TYPE_CHECKING:
    from mappd.settings import Settings

runner = CliRunner()


def test_action_status_covers_every_outbox_status() -> None:
    assert {s.value for s in ActionStatus} == set(actions.STATUSES)


def test_list_empty_says_no_actions(env: Settings) -> None:
    with psycopg.connect(env.database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM actions")
    r = runner.invoke(app, ["action", "list"])
    assert r.exit_code == 0
    assert r.stdout.strip() == "no actions"


@pytest.fixture
def env(migrated: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MAPPD_DATABASE_URL", migrated.database_url)
    return migrated


def unknown(settings: Settings) -> str:
    key = f"demo.send:{uuid4()}"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO actions (kind, idempotency_key, payload, status)"
            " VALUES ('demo.send', %s, '{}', 'unknown')",
            (key,),
        )
    return key


def test_list_json(env: Settings) -> None:
    key = unknown(env)
    r = runner.invoke(app, ["action", "list", "--status", "unknown", "--json"])
    assert r.exit_code == 0, r.stderr
    assert key in {a["idempotency_key"] for a in json.loads(r.stdout)["actions"]}


def test_resolve_took_effect_with_result(env: Settings) -> None:
    key = unknown(env)
    r = runner.invoke(
        app, ["action", "resolve", key, "--took-effect", "--result", '{"id": 3}', "--json"]
    )
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout) == {"idempotency_key": key, "status": "done"}


def test_resolve_unknown_key_exits_1(env: Settings) -> None:
    r = runner.invoke(app, ["action", "resolve", "nope", "--not-sent"])
    assert r.exit_code == 1
    assert "nope" in r.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["action", "resolve", "k"],  # neither flag
        ["action", "resolve", "k", "--took-effect", "--not-sent"],
        ["action", "resolve", "k", "--not-sent", "--result", "{}"],
        ["action", "resolve", "k", "--took-effect", "--result", "not json"],
        ["action", "list", "--status", "stuck"],
    ],
)
def test_usage_errors_exit_2(env: Settings, args: list[str]) -> None:
    assert runner.invoke(app, args).exit_code == 2
