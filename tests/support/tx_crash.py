"""A workflow the transaction tests crash between the app-DB commit and the step record.

Run in a subprocess (`main`): `crash` arms an os._exit inside
SystemDatabase.record_operation_result — the process dies after the datasource
transaction committed but before DBOS records the step in the system database.
`recover` launches the same executor and application version, so DBOS picks the
PENDING workflow back up. URLs and versions come from MAPPD_* env vars.
"""

import os
import sys

import dbos._sys_db
import dbos_async
import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID

from mappd import runtime
from mappd.db import Connection, run
from mappd.db.transaction import transaction
from mappd.settings import Settings

STEP = "test.tx_bump"
WF_ID = "tx-crash-wf"


@transaction(STEP)
def bump(conn: Connection) -> int:
    """version + 1; the checkpoint commits with it."""
    run(conn, sa.text("UPDATE tx_items SET version = version + 1 WHERE id = 'a'"))
    row = run(conn, sa.text("SELECT version FROM tx_items WHERE id = 'a'")).fetchone()
    assert row is not None
    return int(row["version"])


@DBOS.workflow(name="test.tx_crash")
async def tx_crash() -> int:
    return await bump()


async def _run_crash() -> int:
    handle = await DBOS.start_workflow_async(tx_crash)
    return await handle.get_result()


def _arm() -> None:
    original = dbos._sys_db.SystemDatabase.record_operation_result

    def patched(self, result, **kwargs):  # type: ignore[no-untyped-def]
        if result["function_name"] == STEP and result["output"] is not None:
            os._exit(1)
        return original(self, result, **kwargs)

    dbos._sys_db.SystemDatabase.record_operation_result = patched  # type: ignore[method-assign]


def main() -> None:
    runtime.configure(Settings.from_env(), executor_id="tx-crash-executor")
    if sys.argv[1] == "crash":
        _arm()
        DBOS.launch()
        with SetWorkflowID(WF_ID):
            dbos_async.run(_run_crash())
    else:  # recover
        DBOS.launch()
        DBOS.retrieve_workflow(WF_ID).get_result()


if __name__ == "__main__":
    main()
