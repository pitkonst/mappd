"""The RFQ extractor agent and its read-only catalog search tool."""

from pathlib import Path

from mappd import durable, gateway

from ... import catalog
from ...schemas import Extraction

PROMPT_FILE = Path(__file__).parent / "prompt.md"
INSTRUCTIONS = PROMPT_FILE.read_text(encoding="utf-8")

extractor = durable.agent(
    gateway.model("smart"),
    output_type=Extraction,
    instructions=INSTRUCTIONS,
)


@extractor.tool_plain
async def search_catalog(query: str) -> str:
    """Search the catalog; one product per line as `SKU: name (family), price EUR, lead N days`."""
    hits = catalog.search(query)
    return (
        "\n".join(
            f"{h.sku}: {h.name} ({h.family}), {h.price_eur} EUR, lead {h.lead_days} days"
            for h in hits
        )
        or "(no match)"
    )
