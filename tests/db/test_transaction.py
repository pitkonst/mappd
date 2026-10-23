"""`mappd.db.transaction`: a write and its DBOS checkpoint commit in one transaction."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID
from dbos._registrations import get_dbos_func_name

from mappd import db
from mappd.db import Connection, run
from mappd.db import transaction as tx
from mappd.db.migrate import upgrade
from mappd.settings import Settings

ITEMS = sa.table("tx_items", sa.column("id", sa.Text), sa.column("version", sa.Integer))


@pytest.fixture
def tx_settings(dbos_runtime: Settings) -> Settings:
    """The session's DBOS app DB with a scratch table; its datasource is already open."""
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tx_items (id text PRIMARY KEY, version int NOT NULL)"
        )
        conn.execute("INSERT INTO tx_items VALUES ('a', 0) ON CONFLICT DO NOTHING")
        conn.execute("UPDATE tx_items SET version = 0 WHERE id = 'a'")
    return dbos_runtime


def _version(settings: Settings) -> int:
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute("SELECT version FROM tx_items WHERE id = 'a'").fetchone()
    assert row is not None
    return int(row[0])


def _named_write(module: str) -> object:
    """A transaction step whose function claims to live in `module`."""

    def write(conn: Connection) -> None:
        pass

    write.__module__ = module
    return tx.transaction(write)


def test_bare_transaction_derives_process_and_function() -> None:
    assert get_dbos_func_name(_named_write("processes.support.workflow")) == "support.write"
    assert get_dbos_func_name(_named_write("procs_a7.demo.writes")) == "demo.write"


def test_transactions_register_their_names() -> None:
    _named_write("processes.demo.writes")
    assert "demo.write" in tx.TRANSACTIONS


def test_bare_transaction_outside_a_process_package_needs_a_name() -> None:
    with pytest.raises(TypeError, match="explicitly"):
        _named_write("scratchpad")


def test_tx_runs_a_sync_step_in_a_thread() -> None:
    def slow(n: int, *, double: bool = True) -> int:
        return n * 2 if double else n

    assert dbos_async.run(db.tx(slow, 4)) == 8
    assert dbos_async.run(db.tx(slow, 4, double=False)) == 4


def _insert(conn: Connection, note: str) -> None:
    run(conn, sa.text("INSERT INTO tx_items (id, version) VALUES (:id, 0)"), {"id": note})


def _exists(settings: Settings, note: str) -> bool:
    with psycopg.connect(settings.database_url) as conn:
        return conn.execute("SELECT 1 FROM tx_items WHERE id = %s", (note,)).fetchone() is not None


def test_atomic_commits_the_block_and_rolls_back_on_error(tx_settings: Settings) -> None:
    with tx.atomic() as conn:
        _insert(conn, "atomic-ok")

    def explode() -> None:
        with tx.atomic() as conn:
            _insert(conn, "atomic-bad")
            raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        explode()

    assert _exists(tx_settings, "atomic-ok")
    assert not _exists(tx_settings, "atomic-bad")


def test_atomic_inside_a_step_is_a_savepoint(tx_settings: Settings) -> None:
    """An error inside the block rolls it back; the step's earlier writes survive."""

    def explode() -> None:
        with tx.atomic() as conn:
            _insert(conn, "step-dropped")
            raise ValueError("nope")

    @tx.transaction("test.atomic_step")
    def write(conn: Connection) -> None:
        _insert(conn, "step-kept")
        with pytest.raises(ValueError, match="nope"):
            explode()

    dbos_async.run(write())
    assert _exists(tx_settings, "step-kept")
    assert not _exists(tx_settings, "step-dropped")


def test_called_before_open_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tx, "_DS", None)

    @tx.transaction("test.closed")
    def write(conn: Connection) -> None:
        pass

    with pytest.raises(RuntimeError, match="open"):
        dbos_async.run(write())


def test_outside_a_workflow_commits(tx_settings: Settings) -> None:
    @tx.transaction("test.commit")
    def write(conn: Connection, note: str) -> None:
        run(
            conn,
            sa.text("INSERT INTO tx_items (id, version) VALUES (:id, 0)"),
            {"id": note},
        )

    dbos_async.run(write("committed"))
    assert _version(tx_settings) == 0
    with psycopg.connect(tx_settings.database_url) as conn:
        assert conn.execute("SELECT 1 FROM tx_items WHERE id = 'committed'").fetchone()


def test_outside_a_workflow_rolls_back_on_error(tx_settings: Settings) -> None:
    @tx.transaction("test.rollback")
    def write(conn: Connection) -> None:
        run(
            conn,
            sa.text("INSERT INTO tx_items (id, version) VALUES ('rolled-back', 0)"),
        )
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        dbos_async.run(write())
    with psycopg.connect(tx_settings.database_url) as conn:
        assert conn.execute("SELECT 1 FROM tx_items WHERE id = 'rolled-back'").fetchone() is None


def test_inside_a_workflow_write_and_checkpoint_commit_together(tx_settings: Settings) -> None:
    release = threading.Event()
    version_seen: list[int] = []

    @tx.transaction("test.in_wf_bump")
    def bump(conn: Connection) -> int:
        run(conn, sa.text("UPDATE tx_items SET version = version + 1 WHERE id = 'a'"))
        row = run(conn, sa.text("SELECT version FROM tx_items WHERE id = 'a'")).fetchone()
        assert row is not None
        version_seen.append(int(row["version"]))
        return int(row["version"])

    @DBOS.step(name="test.in_wf_wait")
    def wait() -> None:
        release.wait(10)

    @DBOS.workflow(name="test.in_wf")
    async def wf() -> int:
        n = await bump()
        await db.tx(wait)
        return n

    wf_id = f"tx-in-wf-{time.time_ns()}"
    result: list[int] = []

    def run_wf() -> None:
        async def go() -> int:
            handle = await DBOS.start_workflow_async(wf)
            return await handle.get_result()

        with SetWorkflowID(wf_id):
            result.append(dbos_async.run(go()))

    thread = threading.Thread(target=run_wf)
    thread.start()

    # While the second step blocks, the checkpoint row must already be committed in the
    # datasource outputs table — the write and the checkpoint are one transaction.
    deadline = time.monotonic() + 15
    rows: list[tuple[str, int]] = []
    with psycopg.connect(tx_settings.database_url) as conn:
        while time.monotonic() < deadline:
            rows = conn.execute(
                "SELECT workflow_id, step_id FROM dbos.datasource_outputs WHERE workflow_id = %s",
                (wf_id,),
            ).fetchall()
            if rows:
                break
            time.sleep(0.05)
    release.set()
    thread.join(15)
    assert result == [1]
    assert rows == [(wf_id, 1)]
    assert version_seen == [1]
    assert _version(tx_settings) == 1


if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.timeout(60)
def test_crash_between_commit_and_record_recovers_without_rewriting(
    recreate_database: Callable[[str], str],
    tmp_path: Path,
) -> None:
    """os._exit after the tx commit but before the system-DB record: the write runs once."""
    app_url = recreate_database("mappd_test_txcrash")
    sys_url = recreate_database("mappd_test_txcrash_dbos")
    settings = Settings(
        database_url=app_url,
        system_database_url=sys_url,
        app_name="mappd-test",
        app_version="txcrash",
        processes_dir=tmp_path,
    )
    upgrade(settings)
    with psycopg.connect(app_url, autocommit=True) as conn:
        conn.execute("CREATE TABLE tx_items (id text PRIMARY KEY, version int NOT NULL)")
        conn.execute("INSERT INTO tx_items VALUES ('a', 0)")

    env = {
        **os.environ,
        "MAPPD_DATABASE_URL": app_url,
        "MAPPD_SYSTEM_DATABASE_URL": sys_url,
        "MAPPD_APP_NAME": "mappd-test",
        "MAPPD_APP_VERSION": "txcrash",
        "PYTHONPATH": str(Path(__file__).parents[1] / "support"),
    }
    script = str(Path(__file__).parents[1] / "support" / "tx_crash.py")

    crashed = subprocess.run(  # noqa: S603
        [sys.executable, script, "crash"],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert crashed.returncode != 0, crashed.stderr[-2000:]
    assert _version(settings) == 1, "the datasource transaction committed"

    recovered = subprocess.run(  # noqa: S603
        [sys.executable, script, "recover"],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert recovered.returncode == 0, recovered.stderr[-2000:]
    assert _version(settings) == 1, "the recovered step replays its checkpoint, not its body"
