"""Shared fixtures. Tests that need Postgres are marked `pg` automatically."""

import os
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest
import stack
from dbos import DBOS
from fake_gateway import Gateway, running
from psycopg import sql

from mappd import runtime
from mappd.db.migrate import upgrade
from mappd.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from psycopg.rows import TupleRow

ADMIN_URL = os.environ.get(
    "MAPPD_TEST_ADMIN_URL", "postgresql://mappd:mappd@127.0.0.1:5432/postgres"
)
PG_FIXTURES = {
    "pg_settings",
    "fresh_database_url",
    "migrated",
    "dbos_runtime",
    "recreate_database",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test that uses a Postgres fixture as `pg`."""
    for item in items:
        if PG_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.pg)


def _admin() -> psycopg.Connection[TupleRow]:
    try:
        return psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.fail(
            f"Postgres is not reachable at {ADMIN_URL}: {e}\n"
            "Start it with `docker compose up -d --wait postgres`, or deselect with -m 'not pg'.",
            pytrace=False,
        )


def _recreate(name: str) -> str:
    with _admin() as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return f"{ADMIN_URL.rsplit('/', 1)[0]}/{name}"


@pytest.fixture(scope="session")
def pg_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """Settings on fresh app and DBOS test databases, recreated once per session.

    Core migrations only: an empty processes directory keeps the repository's processes out.
    """
    return Settings(
        database_url=_recreate("mappd_test"),
        system_database_url=_recreate("mappd_test_dbos"),
        app_name="mappd-test",
        app_version="test",
        processes_dir=tmp_path_factory.mktemp("no_processes"),
    )


@pytest.fixture
def fresh_database_url() -> str:
    """A database recreated for this test alone (for tests that leave migration state)."""
    return _recreate("mappd_test_fresh")


@pytest.fixture(scope="session")
def migrated(pg_settings: Settings) -> Settings:
    """The session's test database with every core migration applied."""
    upgrade(pg_settings)
    return pg_settings


@pytest.fixture(scope="session")
def dbos_runtime(migrated: Settings) -> Iterator[Settings]:
    """DBOS launched on the test system database for the whole session."""
    runtime.configure(migrated)
    DBOS.launch()
    yield migrated
    DBOS.destroy()


@pytest.fixture
def fake_gateway() -> Iterator[Gateway]:
    """A fake OpenAI-compatible gateway on a free local port."""
    with running() as gateway:
        yield gateway


@pytest.fixture
def recreate_database() -> Callable[[str], str]:
    """Drop and create a database by name; returns its URL. Name test databases mappd_test_*."""
    return _recreate


REPO = Path(__file__).parent


@pytest.fixture
def pg_tools(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """pg_dump and pg_restore that run inside the compose Postgres container."""
    bin_dir = tmp_path_factory.mktemp("pg_bin")
    for tool in ("pg_dump", "pg_restore"):
        script = bin_dir / tool
        script.write_text(
            "#!/bin/sh\n"
            f'exec docker compose -f "{REPO / "compose.yaml"}" exec -T -e PGPASSWORD postgres'
            f' {tool} "$@"\n'
        )
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


@pytest.fixture(scope="session")
def acceptance_stack() -> Iterator[None]:
    """The compose stack for the acceptance scenarios, built once per session."""
    stack.up()
    yield
    if not os.environ.get("MAPPD_KEEP_STACK"):
        stack.down()


@pytest.fixture(autouse=True)
def acceptance_clean(request: pytest.FixtureRequest) -> None:
    """Before every acceptance test: a clean slate and the app at v1."""
    if request.node.get_closest_marker("acceptance") is None:
        return
    request.getfixturevalue("acceptance_stack")
    stack.reset()
    stack.start("v1")


@pytest.fixture(autouse=True)
def no_dotenv(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests never read the developer's `.env` (its mail settings would turn mailboxes on)."""
    if request.node.get_closest_marker("acceptance") is None:
        monkeypatch.setenv("MAPPD_ENV_FILE", os.devnull)
