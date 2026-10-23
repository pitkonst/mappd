"""The fictional world the demo processes live in (world.yaml): curated, reviewed like code."""

from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ValidationError, model_validator

from mappd.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

# An article whose answer depends on the device names the devices it gives different steps for.
MIN_DEVICES = 2

type Topic = Literal["malfunction", "how_to", "billing", "delivery", "refund", "other"]


class WorldError(ValueError):
    """world.yaml cannot be used; the message names the file and the field."""


class Company(BaseModel):
    """The company every demo works for."""

    name: str
    description: str
    languages: list[str]


class Role(BaseModel):
    """A team that takes escalations."""

    id: str
    name: str
    handles: list[str]


class Policy(BaseModel):
    """A business rule the knowledge base and the planners rely on."""

    id: str
    text: str


class Product(BaseModel):
    """A catalog item."""

    sku: str
    family: Literal["controller", "sensor", "camera", "cabinet", "accessory"]
    name: str
    price_eur: float
    lead_days: int


class Article(BaseModel):
    """A knowledge-base article and the customer question it answers."""

    id: str
    part: Literal["technical", "commercial"]
    topic: Topic
    title: str
    body: str
    question: str
    # What the answer depends on: a question about one of these, asked without it, needs a clarify.
    needs: list[Literal["order", "site", "device"]] = []


class Contact(BaseModel):
    """Who lets an installer in."""

    name: str
    phone: str


class Site(BaseModel):
    """A customer site where installers work."""

    name: str
    access: str
    contact: Contact


class Depot(BaseModel):
    """A warehouse and what it has on hand, by SKU."""

    name: str
    stock: dict[str, int]


class DeliverySlot(BaseModel):
    """A delivery window logistics can book; `site=None` serves any site."""

    site: str | None = None
    at: str


class Persona(BaseModel):
    """How a simulated installer writes."""

    id: str
    style: str


class World(BaseModel):
    """The whole curated world.

    The installation sections (sites, depots, delivery slots, personas) are optional, so a
    world for the other demos needs none of them.
    """

    company: Company
    roles: list[Role]
    policies: list[Policy]
    catalog: list[Product]
    knowledge_base: list[Article]
    sites: list[Site] = []
    depots: list[Depot] = []
    delivery_slots: list[DeliverySlot] = []
    personas: list[Persona] = []

    @model_validator(mode="after")
    def _unique_ids(self) -> World:
        for label, values in (
            ("role", [r.id for r in self.roles]),
            ("sku", [p.sku for p in self.catalog]),
            ("article", [a.id for a in self.knowledge_base]),
            ("site", [s.name for s in self.sites]),
            ("depot", [d.name for d in self.depots]),
            ("persona", [p.id for p in self.personas]),
        ):
            seen: set[str] = set()
            for value in values:
                if value in seen:
                    raise ValueError(f"duplicate {label} {value}")
                seen.add(value)
        return self

    @model_validator(mode="after")
    def _device_needs_name_devices(self) -> World:
        for i, article in enumerate(self.knowledge_base):
            if "device" in article.needs and len(self.products_in(article)) < MIN_DEVICES:
                raise ValueError(
                    f"knowledge_base[{i}].body: an article that needs the device must give"
                    " different steps for at least two catalog products, by name"
                )
        return self

    @model_validator(mode="after")
    def _installation_names_resolve(self) -> World:
        skus = {p.sku for p in self.catalog}
        for depot in self.depots:
            for sku in depot.stock:
                if sku not in skus:
                    raise ValueError(f"depot {depot.name}: unknown sku {sku}")
        sites = {s.name for s in self.sites}
        for slot in self.delivery_slots:
            if slot.site is not None and slot.site not in sites:
                raise ValueError(f"delivery slot {slot.at}: unknown site {slot.site}")
        return self

    def site(self, name: str) -> Site:
        """The site with this name."""
        for site in self.sites:
            if site.name == name:
                return site
        raise WorldError(f"no site {name!r} in the world")

    def products_in(self, article: Article) -> list[Product]:
        """Catalog products the article's body names."""
        return [p for p in self.catalog if p.name in article.body]

    def role(self, role_id: str) -> Role:
        """The role with this id."""
        for role in self.roles:
            if role.id == role_id:
                return role
        raise WorldError(f"no role {role_id!r} in the world")

    def articles(
        self, *, part: str | None = None, exclude_topics: tuple[str, ...] = ()
    ) -> list[Article]:
        """Articles of one part (or all), without the excluded topics."""
        return [
            a
            for a in self.knowledge_base
            if (part is None or a.part == part) and a.topic not in exclude_topics
        ]


def _location(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def load(path: Path | None = None) -> World:
    """Read and validate world.yaml (defaults to `Settings.from_env().world_path`).

    Raises:
        WorldError: the file is missing, not YAML, or invalid; names the field.
    """
    resolved = path or Settings.from_env().world_path
    try:
        data = yaml.safe_load(resolved.read_text())
    except OSError as e:
        raise WorldError(f"cannot read {resolved}: {e}") from None
    except yaml.YAMLError as e:
        raise WorldError(f"{resolved} is not valid YAML: {e}") from None
    try:
        return World.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        where = _location(tuple(first["loc"])) or "world"
        raise WorldError(f"{resolved}: {where}: {first['msg']}") from None
