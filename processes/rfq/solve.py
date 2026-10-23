"""The RFQ decision for one request: the agent reads, the domain decides."""

from .agents.extractor import extractor
from .domain import solve_output
from .schemas import RfqIn, RfqOutput


def request_text(rfq: RfqIn) -> str:
    """What the extractor reads."""
    return f"Subject: {rfq.subject}\n\n{rfq.body}"


async def solve(rfq: RfqIn) -> RfqOutput:
    """Extract the lines with the agent, then price, gate and draft in code."""
    extraction = (await extractor.run(request_text(rfq))).output
    return solve_output(extraction)
