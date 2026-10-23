"""First-line support: classify, escalate by rule, draft from the knowledge base, approve, send."""

from datetime import timedelta
from pathlib import Path

from mappd.autonomy import categories
from mappd.ops import Cases
from mappd.process import MockSpec, Process
from mappd.settings import environment

from . import domain, feedback
from .api import router
from .data.seed import seed
from .data.synth import spec as synth_spec
from .db import TICKETS
from .evals import spec as eval_spec
from .events import WEBHOOKS
from .mail import MAILBOX
from .mocks import mock_llm, mock_webhook
from .pages import router as page_router
from .schemas import Classification
from .service import ingest

# Tests and the acceptance stack point MAPPD_SUPPORT_AUTONOMY at their own policy file.
AUTONOMY = Path(
    environment().get("MAPPD_SUPPORT_AUTONOMY") or Path(__file__).parent / "autonomy.yaml"
)

process = Process(
    name="support",
    router=router,
    pages=page_router,
    role="operator",
    webhooks=WEBHOOKS,
    autonomy=AUTONOMY,
    dashboard="/support/dashboard",
    cases=Cases(
        table=TICKETS,
        link="/support/tickets/{key}",
        waiting=("awaiting_approval", "escalated"),
        # Drafting and sending take seconds; ten minutes means a failed workflow.
        passing={"new": timedelta(minutes=10), "approved": timedelta(minutes=10)},
    ),
    feedback=feedback.spec,
    categories=tuple(
        c for c in categories(Classification, "topic") if c not in domain.ALWAYS_ESCALATE
    ),
    ingest=ingest,
    migrations_dir=Path(__file__).parent / "migrations",
    seed=seed,
    seeds="knowledge-base articles",
    evals=eval_spec,
    synth=synth_spec,
    mailboxes=(MAILBOX,),
    mocks={
        "llm": MockSpec(app=mock_llm.app, port=mock_llm.PORT),
        "webhook": MockSpec(app=mock_webhook.app, port=mock_webhook.PORT),
    },
)
