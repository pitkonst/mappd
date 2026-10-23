"""The catalog as the RFQ process reads it: world.yaml, searched by keywords."""

import re
from decimal import Decimal

from mappd.data.world import load

from .schemas import CatalogHit

PRODUCTS: dict[str, CatalogHit] = {
    p.sku: CatalogHit(
        sku=p.sku,
        name=p.name,
        family=p.family,
        price_eur=Decimal(str(p.price_eur)).quantize(Decimal("0.01")),
        lead_days=p.lead_days,
    )
    for p in load().catalog
}
WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return set(WORD.findall(text.lower()))


def search(query: str, limit: int = 5) -> list[CatalogHit]:
    """Products sharing the most words with `query` (name, family, SKU parts), best first."""
    wanted = _words(query)
    scored = []
    for hit in PRODUCTS.values():
        have = _words(f"{hit.name} {hit.family} {hit.sku.replace('-', ' ')}")
        score = len(wanted & have)
        if score:
            scored.append((-score, hit.sku, hit))
    return [h for _, _, h in sorted(scored)[:limit]]
