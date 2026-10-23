"""Support's events and webhook targets."""

from typing import TYPE_CHECKING

from mappd.events import Event, WebhookTarget

if TYPE_CHECKING:
    from .schemas import SupportOutput, TicketRecord

WEBHOOKS = (WebhookTarget("default", "MAPPD_SUPPORT_WEBHOOK_URL", "MAPPD_SUPPORT_WEBHOOK_SECRET"),)


def event(
    type: str,  # noqa: A002 - the event's field name
    ticket: TicketRecord,
    version: int,
    status: str,
    output: SupportOutput | None = None,
    *,
    escalate_to: str | None = None,
) -> Event:
    """An event about `ticket`: enough for a help desk to route it without a second call.

    `escalate_to` names the team an operator escalated to (the agent's output did not).
    """
    return Event.of(
        "support",
        ticket.external_id,
        type,
        version,
        {
            "status": status,
            "shadow": status == "shadowed",
            "outcome": "escalate" if escalate_to else output.outcome if output else None,
            "escalate_to": escalate_to or (output.escalate_to if output else None),
            "subject": ticket.subject,
            "sender": ticket.sender,
        },
    )
