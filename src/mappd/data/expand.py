"""Seeded expansion of the world into customers, sites and orders: a pure function of the seed.

Every name is invented from the lists below; emails use the reserved .example domain.
"""

import random
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from mappd.data.world import World

type Tone = Literal["formal", "casual", "annoyed"]

STYLES: tuple[tuple[str, Tone], ...] = (
    ("terse procurement buyer", "formal"),
    ("detailed facility manager", "formal"),
    ("stressed site technician", "casual"),
    ("polite office assistant", "formal"),
    ("annoyed long-time customer", "annoyed"),
    ("non-technical building owner", "casual"),
)
FIRST_NAMES = ("Anna", "Jonas", "Mira", "Lukas", "Sofia", "Elias", "Lena", "Noah", "Clara", "Finn")
LAST_NAMES = ("Berger", "Hoffmann", "Novak", "Keller", "Lindqvist", "Moreau", "Brandt", "Okafor")
COMPANY_WORDS = ("Nordlicht", "Alder", "Brightwater", "Kestrel", "Juniper", "Halden", "Marlow")
COMPANY_KINDS = ("Facility Services", "Property Management", "Hotels", "Logistics", "Clinics")
CITIES = ("Hamburg", "Leipzig", "Graz", "Basel", "Utrecht", "Malmo", "Lyon", "Bristol")
BUILDINGS = ("Warehouse 2", "Main office", "North wing", "Plant B", "Depot", "Clinic annex")


class Persona(BaseModel):
    """How a customer writes; one per customer, so a split by persona is a split by customer."""

    id: str
    style: str
    tone: Tone
    typo_rate: float


class Customer(BaseModel):
    """A customer contact."""

    id: str
    company: str
    contact: str
    email: str
    language: Literal["en", "de"]
    persona: Persona


class Site(BaseModel):
    """A building where equipment is installed."""

    id: str
    customer_id: str
    city: str
    building: str


class Order(BaseModel):
    """An order of catalog items for a site."""

    id: str
    customer_id: str
    site_id: str
    skus: list[str]
    status: Literal["placed", "shipped", "delivered", "installed"]


class Expansion(BaseModel):
    """Everything expanded from one seed."""

    customers: list[Customer]
    sites: list[Site]
    orders: list[Order]

    def sites_of(self, customer_id: str) -> list[Site]:
        """The customer's sites."""
        return [s for s in self.sites if s.customer_id == customer_id]

    def orders_of(self, customer_id: str) -> list[Order]:
        """The customer's orders."""
        return [o for o in self.orders if o.customer_id == customer_id]


def expand(world: World, seed: int, *, customers: int = 40, german_share: float = 0.3) -> Expansion:
    """Customers with their sites and orders, reproducible from `seed`."""
    rng = random.Random(seed)
    skus = [p.sku for p in world.catalog]
    out = Expansion(customers=[], sites=[], orders=[])
    for n in range(customers):
        style, tone = rng.choice(STYLES)
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        company = f"{rng.choice(COMPANY_WORDS)} {rng.choice(COMPANY_KINDS)}"
        customer_id = f"c{seed}-{n:03d}"
        out.customers.append(
            Customer(
                id=customer_id,
                company=company,
                contact=f"{first} {last}",
                email=f"{first.lower()}.{last.lower()}@{company.split(maxsplit=1)[0].lower()}.example",
                language="de" if rng.random() < german_share else "en",
                persona=Persona(
                    id=f"{customer_id}-{style.split()[0]}",
                    style=style,
                    tone=tone,
                    typo_rate=round(rng.uniform(0.0, 0.03), 3),
                ),
            )
        )
        for s in range(rng.randint(1, 2)):
            site_id = f"{customer_id}-s{s}"
            out.sites.append(
                Site(
                    id=site_id,
                    customer_id=customer_id,
                    city=rng.choice(CITIES),
                    building=rng.choice(BUILDINGS),
                )
            )
            out.orders.append(
                Order(
                    id=f"ORD-{rng.randint(10000, 99999)}",
                    customer_id=customer_id,
                    site_id=site_id,
                    skus=rng.sample(skus, rng.randint(1, 3)),
                    status=rng.choice(("placed", "shipped", "delivered", "installed")),
                )
            )
    return out
