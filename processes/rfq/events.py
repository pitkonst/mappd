"""RFQ's events and webhook target (as support's)."""

from typing import TYPE_CHECKING

from mappd.events import Event, WebhookTarget

if TYPE_CHECKING:
    from .schemas import RfqOutput, RfqRecord

WEBHOOKS = (WebhookTarget("default", "MAPPD_RFQ_WEBHOOK_URL", "MAPPD_RFQ_WEBHOOK_SECRET"),)


def event(
    type: str,  # noqa: A002 - the event's field name
    rfq: RfqRecord,
    version: int,
    status: str,
    output: RfqOutput | None = None,
    *,
    escalate_to: str | None = None,
) -> Event:
    """An event about `rfq`: enough for a CRM to route it without a second call.

    `escalate_to` names the team an operator escalated to (the agent's output did not).
    """
    return Event.of(
        "rfq",
        rfq.external_id,
        type,
        version,
        {
            "status": status,
            "shadow": status == "shadowed",
            "outcome": "escalate" if escalate_to else output.outcome if output else None,
            "escalate_to": escalate_to or (output.escalate_to if output else None),
            "total": str(output.total) if output else None,
            "subject": rfq.subject,
            "sender": rfq.sender,
        },
    )
