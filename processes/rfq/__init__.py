"""RFQ processing: requests for quotation become priced quotes."""

from datetime import timedelta
from pathlib import Path

from mappd.ops import Cases
from mappd.process import MockSpec, Process
from mappd.settings import environment

from . import feedback
from .api import router
from .data.synth import spec as synth_spec
from .db import RFQS
from .evals import spec as eval_spec
from .events import WEBHOOKS
from .mail import MAILBOX
from .mocks import mock_llm
from .pages import router as page_router
from .service import ingest

# Tests and the acceptance stack point MAPPD_RFQ_AUTONOMY at their own policy file.
AUTONOMY = Path(environment().get("MAPPD_RFQ_AUTONOMY") or Path(__file__).parent / "autonomy.yaml")

process = Process(
    name="rfq",
    router=router,
    pages=page_router,
    dashboard="/rfq/dashboard",
    cases=Cases(
        table=RFQS,
        link="/rfq/requests/{key}",
        waiting=("awaiting_approval", "escalated"),
        # Extraction and sending take seconds; ten minutes means a failed workflow.
        passing={"new": timedelta(minutes=10), "approved": timedelta(minutes=10)},
    ),
    feedback=feedback.spec,
    synth=synth_spec,
    evals=eval_spec,
    mailboxes=(MAILBOX,),
    mocks={"llm": MockSpec(app=mock_llm.app, port=9000, claims=mock_llm.claims)},
    webhooks=WEBHOOKS,
    ingest=ingest,
    autonomy=AUTONOMY,
    categories=("quote", "clarify"),
    migrations_dir=Path(__file__).parent / "migrations",
)
