"""Stand-ins for the systems an installation exception touches.

Warehouse, logistics, tickets and contacts. One class answers both the mock MCP server
(`mock_mcp.py`) and the evals (an in-process toolset), so the tools' names and signatures are
the same in both.

Mutating tools take an idempotency `key` and honor it: the same key returns the first answer
and changes nothing again.
"""

import threading
from typing import Any

from pydantic import BaseModel
from pydantic_ai.toolsets import FunctionToolset

from mappd.data.world import World

TOOLS: dict[str, tuple[str, ...]] = {
    "warehouse": ("check_stock", "reserve_parts"),
    "logistics": ("delivery_slots", "book_delivery"),
    "tickets": ("open_ticket",),
    "contacts": ("site_contact",),
}
# Commitments a dispatcher confirms: the resolver only proposes them (AGENTS.md, rule 6).
CONFIRMED = frozenset({"reserve_parts", "book_delivery"})
# Mutating tools take an idempotency key; the caller supplies it, never the model (tools.py).
NO_KEY: dict[str, Any] = {"ok": False, "error": "an idempotency key is required"}


class Contact(BaseModel):
    """A site's contact and how to get in."""

    name: str
    phone: str
    access: str


class Slot(BaseModel):
    """A delivery window; `site=None` serves any site."""

    site: str | None = None
    at: str


class Facts(BaseModel):
    """What the systems know: stock on hand, free delivery slots, site contacts."""

    stock: dict[str, int] = {}
    slots: list[Slot] = []
    contacts: dict[str, Contact] = {}
    # Units found broken on site: the simulator reports them, no tool reveals them.
    damaged: list[str] = []

    @classmethod
    def from_world(cls, world: World) -> Facts:
        """Stock summed over the depots, the world's slots and site contacts."""
        stock: dict[str, int] = {}
        for depot in world.depots:
            for sku, n in depot.stock.items():
                stock[sku] = stock.get(sku, 0) + n
        return cls(
            stock=stock,
            slots=[Slot(site=s.site, at=s.at) for s in world.delivery_slots],
            contacts={
                s.name: Contact(name=s.contact.name, phone=s.contact.phone, access=s.access)
                for s in world.sites
            },
        )


class Systems:
    """The four systems over one set of facts; `calls` records every tool call."""

    def __init__(self, facts: Facts) -> None:
        self.facts = facts.model_copy(deep=True)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._done: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._seq = 0

    def _record(self, tool: str, arguments: dict[str, Any]) -> None:
        self.calls.append((tool, arguments))

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def check_stock(self, sku: str) -> dict[str, Any]:
        """How many units of `sku` the warehouse has on hand."""
        with self._lock:
            self._record("check_stock", {"sku": sku})
            return {"sku": sku, "on_hand": self.facts.stock.get(sku, 0)}

    def reserve_parts(self, job: str, skus: list[str], key: str = "") -> dict[str, Any]:
        """Reserve one unit of each SKU for a job; refused when any is out of stock."""
        with self._lock:
            self._record("reserve_parts", {"job": job, "skus": skus, "key": key})
            if not key:
                return NO_KEY
            if key in self._done:
                return self._done[key]
            missing = [s for s in skus if self.facts.stock.get(s, 0) < skus.count(s)]
            if missing:
                return {"ok": False, "missing": sorted(set(missing))}
            for sku in skus:
                self.facts.stock[sku] -= 1
            result = {"ok": True, "reservation": self._next("R"), "job": job, "skus": skus}
            self._done[key] = result
            return result

    def delivery_slots(self, site: str, skus: list[str]) -> dict[str, Any]:
        """Free delivery windows for a site (its own and those serving any site)."""
        with self._lock:
            self._record("delivery_slots", {"site": site, "skus": skus})
            free = [s.at for s in self.facts.slots if s.site in {site, None}]
            return {"site": site, "slots": free}

    def book_delivery(self, job: str, slot: str, key: str = "") -> dict[str, Any]:
        """Book a free delivery window for a job."""
        with self._lock:
            self._record("book_delivery", {"job": job, "slot": slot, "key": key})
            if not key:
                return NO_KEY
            if key in self._done:
                return self._done[key]
            match = next((s for s in self.facts.slots if s.at == slot), None)
            if match is None:
                return {"ok": False, "error": f"no free slot at {slot}"}
            self.facts.slots.remove(match)
            result = {"ok": True, "booking": self._next("B"), "job": job, "slot": slot}
            self._done[key] = result
            return result

    def open_ticket(self, job: str, kind: str, text: str, key: str = "") -> dict[str, Any]:
        """Open a service ticket (warranty, breakage) for a job."""
        with self._lock:
            self._record("open_ticket", {"job": job, "kind": kind, "text": text, "key": key})
            if not key:
                return NO_KEY
            if key in self._done:
                return self._done[key]
            result = {"ok": True, "ticket": self._next("T"), "job": job, "kind": kind}
            self._done[key] = result
            return result

    def site_contact(self, site: str) -> dict[str, Any]:
        """Who to call at a site, and how to get in."""
        with self._lock:
            self._record("site_contact", {"site": site})
            contact = self.facts.contacts.get(site)
            if contact is None:
                return {"site": site, "error": "unknown site"}
            return {"site": site, **contact.model_dump()}


def toolset(systems: Systems, *, include_confirmed: bool = False) -> FunctionToolset[Any]:
    """The systems' tools in process (evals); confirmed tools only when asked for."""
    names = [t for tools in TOOLS.values() for t in tools]
    chosen = [n for n in names if include_confirmed or n not in CONFIRMED]
    return FunctionToolset(tools=[getattr(systems, n) for n in chosen])
