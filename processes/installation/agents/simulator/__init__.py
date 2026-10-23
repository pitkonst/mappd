"""The simulated technician of the dialogue-simulation evals (gateway model `simulator`).

It plays a persona through a scenario's script one step per turn: what has just happened and
the planned line come from the script, the harness decides when photos go out, and the model
only words the message — answering the bot's questions from what the technician knows.
"""

from pathlib import Path

from pydantic_ai import Agent

from mappd import gateway

from ...sim import Scenario, Step

PROMPT_FILE = Path(__file__).parent / "prompt.md"
INSTRUCTIONS = PROMPT_FILE.read_text(encoding="utf-8")

# A plain agent: the simulator runs in evals only, never inside a workflow.
agent: Agent[None, str] = Agent(
    gateway.model("simulator"), name="installation.simulator", instructions=INSTRUCTIONS
)


def message_text(scenario: Scenario, step: Step, bot_reply: str, *, photo: bool) -> str:
    """What the simulator reads for one turn."""
    contact = scenario.facts.contacts.get(scenario.job.site)
    known = [f"Site: {scenario.job.site}"]
    if contact is not None:
        known.append(f"Site contact: {contact.name}, {contact.phone}. {contact.access}")
    if scenario.facts.damaged:
        known.append(f"Damaged on arrival: {', '.join(scenario.facts.damaged)}")
    return "\n".join(
        [
            f"Style: {scenario.persona}",
            f"What just happened: {step.fact or step.line}",
            f"Planned message: {step.line}",
            f"Photo with this message: {'yes' if photo else 'no'}",
            f"Bot's last reply: {bot_reply or '(none yet)'}",
            *known,
        ]
    )


async def speak(scenario: Scenario, step: Step, bot_reply: str, *, photo: bool) -> str:
    """The technician's next message."""
    result = await agent.run(message_text(scenario, step, bot_reply, photo=photo))
    return result.output
