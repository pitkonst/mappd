"""Installation support: a job is a case driven by the technician's Telegram messages."""

from pathlib import Path

from mappd.ops import Cases
from mappd.process import MockSpec, Process, Schedule

from . import telegram, workflow
from .api import router
from .data.synth import spec as synth_spec
from .db import JOBS, NEEDS_DISPATCHER
from .evals import spec as eval_spec
from .mocks import mock_llm, mock_mcp, mock_telegram
from .pages import router as page_router

# Long polling for development and the acceptance stack; production uses the webhook.
SCHEDULES = (
    Schedule(name="installation.deadlines", workflow=workflow.check_deadlines, cron="0 * * * * *"),
    *(
        (Schedule(name="installation.poll", workflow=workflow.poll, cron="*/5 * * * * *"),)
        if telegram.mode() == "polling"
        else ()
    ),
)

process = Process(
    name="installation",
    router=router,
    pages=page_router,
    role="dispatcher",
    # Telegram's webhook verifies its own secret token; it cannot sign in.
    public=("/api/v1/installation/inbound/telegram",),
    schedules=SCHEDULES,
    # A job moves at the technician's pace, so no state is "passing"; a block waits for a
    # dispatcher.
    cases=Cases(
        table=JOBS,
        link="/installation/jobs/{key}",
        waiting=("blocked",),
        also_waiting=NEEDS_DISPATCHER,
        status="state",
    ),
    synth=synth_spec,
    evals=eval_spec,
    migrations_dir=Path(__file__).parent / "migrations",
    mocks={
        "llm": MockSpec(app=mock_llm.app, port=9000, claims=mock_llm.claims),
        "telegram": MockSpec(app=mock_telegram.app, port=mock_telegram.PORT),
        "mcp": MockSpec(app=mock_mcp.app, port=mock_mcp.PORT),
    },
)
