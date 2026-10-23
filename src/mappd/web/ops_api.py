"""Core API: the Operations view (`/api/v1/ops`); the page calls the same service."""

from fastapi import APIRouter, Request

from mappd import ops
from mappd.web import workflows_api

router = APIRouter(prefix="/api/v1/ops", tags=["ops"])


def declared(request: Request) -> list[tuple[str, ops.Cases]]:
    """The registered processes that declare their cases, in registration order."""
    found: list[tuple[str, ops.Cases]] = request.app.state.cases
    return found


async def full_report(request: Request) -> ops.OpsReport:
    """The cases and the outbox, plus failed and stranded workflows: the API and the page."""
    report = await ops.areport(declared(request))
    stats = await workflows_api.get_stats(workflows_api.settings_of(request), "24h")
    failed = stats.by_status.get("ERROR", 0) + stats.by_status.get(
        "MAX_RECOVERY_ATTEMPTS_EXCEEDED", 0
    )
    flows = ops.WorkflowOps(failed=failed, stranded=stats.stranded)
    return report.model_copy(update={"workflows": flows})


@router.get("")
async def get_ops(request: Request) -> ops.OpsReport:
    """Cases waiting and stuck per process, the outbox's trouble, failed and stranded workflows.

    Failed counts the last 24 hours.
    """
    return await full_report(request)
