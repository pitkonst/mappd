"""Workflows the workflow tests strand, recover and cancel.

The test session imports this module, which registers the workflows before DBOS launches. A
subprocess runs `main()` on another application version and is killed after the first step:
the workflow stays PENDING on that version, as after a hard crash right before a deploy.
"""

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from dbos import DBOS, SetWorkflowID

from mappd import runtime, workflows
from mappd.actions import AUTO, action

if TYPE_CHECKING:
    from dbos import Queue
from mappd.settings import Settings

STALL = "MAPPD_TEST_STALL"
EXECUTOR = "MAPPD_TEST_EXECUTOR_ID"
HERE = Path(__file__).parent
DASHBOARD_QUEUE = "test-dashboard"


@DBOS.step(name="test.stranded.first")
def first() -> str:
    return "one"


@DBOS.step(name="test.stranded.second")
def second() -> str:
    if os.environ.get(STALL):
        time.sleep(60)
    return "two"


@DBOS.workflow(name="test.stranded")
def stranded_workflow() -> str:
    first()
    return second()


@DBOS.step(name="test.flaky.step", retries_allowed=False)
def flaky() -> str:
    if os.environ.get(STALL):
        raise RuntimeError("downstream is down")
    return "ok"


@DBOS.workflow(name="test.flaky")
def flaky_workflow() -> str:
    first()
    return flaky()


def fail() -> str:
    """Run flaky_workflow with its second step failing; it ends in ERROR on this version."""
    workflow_id = f"flaky-{uuid4()}"
    os.environ[STALL] = "1"
    try:
        with SetWorkflowID(workflow_id), contextlib.suppress(RuntimeError):
            DBOS.start_workflow(flaky_workflow).get_result()
    finally:
        del os.environ[STALL]
    return workflow_id


@DBOS.workflow(name="test.waiting")
def waiting_workflow() -> str | None:
    message = DBOS.recv("go", timeout_seconds=30)
    return None if message is None else str(message)


@DBOS.workflow(name="test.child")
def child_workflow() -> str:
    return first()


@DBOS.workflow(name="test.parent")
def parent_workflow() -> str:
    return DBOS.start_workflow(child_workflow).get_result()


SENT: list[str] = []


@DBOS.step(name="test.sender.first")
async def sender_first() -> str:
    return "first"


@action("test.sender.send", on_unknown="resend")
def deliver(idempotency_key: str, run_id: str) -> dict[str, str]:
    SENT.append(run_id)
    return {"sent": run_id}


@DBOS.step(name="test.sender.last")
async def sender_last() -> str:
    return "last"


@DBOS.workflow(name="test.sender")
async def sender_workflow(run_id: str, auto: bool = False) -> str:
    """Three steps; the second sends through the outbox, keyed by business key or AUTO."""
    await sender_first()
    await deliver(AUTO if auto else f"test.sender:{run_id}", run_id)
    return await sender_last()


@DBOS.workflow(name="test.sender.twice")
async def sender_twice(run_id: str) -> str:
    """Two sends of one action kind in one workflow: each step has its own outbox row."""
    await deliver(f"test.sender:{run_id}:a", run_id)
    await deliver(f"test.sender:{run_id}:b", run_id)
    return "twice"


def dashboard_queue() -> Queue:
    """One at a time: a second enqueued workflow waits while the first runs (queue depth)."""
    return DBOS.retrieve_queue(DASHBOARD_QUEUE) or DBOS.register_queue(
        DASHBOARD_QUEUE, concurrency=1
    )


def main() -> None:
    runtime.configure(Settings.from_env(), executor_id=os.environ.get(EXECUTOR))
    DBOS.launch()
    with SetWorkflowID(sys.argv[1]):
        DBOS.start_workflow(stranded_workflow)
    time.sleep(60)


def strand(
    settings: Settings,
    version: str,
    app_name: str | None = None,
    executor_id: str | None = None,
) -> str:
    """Start stranded_workflow on `version` in a subprocess and kill it after the first step.

    DBOS executors only run workflows of their own application, so the app name matters.
    """
    workflow_id = f"stranded-{uuid4()}"
    env = {
        **os.environ,
        "MAPPD_DATABASE_URL": settings.database_url,
        "MAPPD_SYSTEM_DATABASE_URL": settings.system_database_url,
        "MAPPD_APP_VERSION": version,
        "MAPPD_APP_NAME": app_name or settings.app_name,
        "PYTHONPATH": str(HERE),
        STALL: "1",
        **({EXECUTOR: executor_id} if executor_id else {}),
    }
    proc = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-c", "import stranded; stranded.main()", workflow_id],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        with workflows.client(settings) as c:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                steps = c.list_workflow_steps(workflow_id, load_output=False)
                if any(s["function_name"] == "test.stranded.first" for s in steps):
                    return workflow_id
                time.sleep(0.2)
    finally:
        proc.kill()
        _, err = proc.communicate()
    raise AssertionError(f"{workflow_id} never finished its first step: {err.decode()[-2000:]}")
