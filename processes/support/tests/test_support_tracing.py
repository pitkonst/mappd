"""approve_and_send is one trace: the DBOS workflow span and every step span, the send included."""

import uuid
from typing import TYPE_CHECKING

import dbos_async
import psycopg
import pytest
import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID
from fake_mail import install
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from mappd.db.migrate import sqlalchemy_url
from mappd.settings import Settings
from processes.support import domain, workflow
from processes.support.db import TICKETS

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan


@pytest.fixture
def ticket(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> str:
    """An 'awaiting_approval' ticket at version 0 in the session DB."""
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    engine = sa.create_engine(sqlalchemy_url(dbos_runtime.database_url))
    try:
        TICKETS.create(engine, checkfirst=True)
    finally:
        engine.dispose()
    external_id = f"trace-{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dbos_runtime.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO support_tickets (external_id, sender, subject, body, status)"
            " VALUES (%s, 'anna@example.com', 'Sensor offline', 'Room 3', 'awaiting_approval')",
            (external_id,),
        )
    return external_id


def in_memory_exporter() -> InMemorySpanExporter:
    """Attach an in-memory exporter to the global provider DBOS spans go to.

    OTel allows set_tracer_provider once: install an SDK provider only when the global
    is still the pre-setup proxy, and add the processor to it either way.
    """
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if isinstance(provider, trace.ProxyTracerProvider):
        sdk = TracerProvider()
        trace.set_tracer_provider(sdk)
        provider = sdk
    assert isinstance(provider, TracerProvider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def test_approve_and_send_exports_workflow_and_steps_in_one_trace(
    dbos_runtime: Settings, ticket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter = in_memory_exporter()
    _, smtp = install(monkeypatch, "support")

    async def invoke() -> str:
        with SetWorkflowID(domain.approval_workflow_id(ticket, 0)):
            handle = await DBOS.start_workflow_async(
                workflow.approve_and_send, ticket, 0, "olga", "We rebooted the sensor."
            )
        return await handle.get_result()

    assert dbos_async.run(invoke()) == "sent"
    assert len(smtp.sent) == 1

    spans: dict[str, ReadableSpan] = {s.name: s for s in exporter.get_finished_spans()}
    assert len({s.context.trace_id for s in spans.values()}) == 1, "one trace"
    expected = [
        "support.approve_and_send",
        "support.load_ticket",
        "support.claim_approval",
        "mappd.send_email",
        "support.mark_sent",
    ]
    assert set(expected) <= set(spans), f"missing spans: {set(expected) - set(spans)}"
    workflow_span = spans["support.approve_and_send"]
    for name in expected[1:]:
        parent = spans[name].parent
        assert parent is not None
        assert parent.span_id == workflow_span.context.span_id, f"{name} is a workflow child"
    wf_attrs = dict(workflow_span.attributes or {})
    assert wf_attrs["mlflow.traceTag.mlflow.traceName"] == "support.approve_and_send"
    assert wf_attrs["user.id"] == "olga"
    assert wf_attrs["session.id"] == ticket
    assert "mlflow.spanInputs" in wf_attrs
    assert "mlflow.spanOutputs" in wf_attrs
