"""The interpreter agent: what a technician's message means for the job."""

from pathlib import Path

from mappd import durable, gateway

from ...engine import JobView
from ...schemas import Interpretation, State

PROMPT_FILE = Path(__file__).parent / "prompt.md"
INSTRUCTIONS = PROMPT_FILE.read_text(encoding="utf-8")
# The output is checked (an exception names its kind): room for the model to correct itself.
OUTPUT_RETRIES = 3
# The messages it reads before the new one: enough to keep the language and what was said.
HISTORY = 10

interpreter = durable.agent(
    gateway.model("smart"),
    output_type=Interpretation,
    instructions=INSTRUCTIONS,
    retries=OUTPUT_RETRIES,
)


def message_text(job: JobView, next_state: State | None, text: str, photo: str | None) -> str:
    """What the interpreter reads: the job, the recent conversation, then the new message.

    Without the job and the conversation it guessed the equipment, forgot the technician's
    language between messages and could not say when the appointment was.
    """
    lines = [
        f"Site: {job.site}",
        f"Equipment: {', '.join(job.skus) or '(none listed)'}",
        f"Appointment: {job.appointment or 'unknown'}",
        f"Current step: {job.state}",
        f"Next step: {next_state or 'none (the job is closed)'}",
        "Conversation so far:",
        *(f"- {line}" for line in job.history[-HISTORY:]),
    ]
    if not job.history:
        lines.append("- (none)")
    if photo:
        lines.append(f"Photo: {photo}")
    lines.append(f"Message: {text or '(no text)'}")
    return "\n".join(lines)


async def interpret(
    job: JobView, next_state: State | None, text: str, photo: str | None
) -> Interpretation:
    """The interpreter's reading of one message (each model call is a journaled step)."""
    return (await interpreter.run(message_text(job, next_state, text, photo))).output
