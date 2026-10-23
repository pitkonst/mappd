"""The support classifier agent: topic, route, urgency, language, safety risk, and outcome."""

from pathlib import Path

from mappd import durable, gateway

from ...schemas import Classification

PROMPT_FILE = Path(__file__).parent / "prompt.md"
INSTRUCTIONS = PROMPT_FILE.read_text(encoding="utf-8")

classifier = durable.agent(
    gateway.model("fast"),
    output_type=Classification,
    instructions=INSTRUCTIONS,
)
