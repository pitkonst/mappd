from pathlib import Path

from mappd.data.expand import expand
from mappd.data.world import load

WORLD = load(Path(__file__).parents[2] / "world.yaml")


def test_same_seed_same_expansion() -> None:
    assert expand(WORLD, 42) == expand(WORLD, 42)
    assert expand(WORLD, 42) != expand(WORLD, 43)


def test_every_customer_has_a_site_an_order_and_its_own_persona() -> None:
    e = expand(WORLD, 7, customers=30)
    assert len(e.customers) == 30
    assert len({c.persona.id for c in e.customers}) == 30
    skus = {p.sku for p in WORLD.catalog}
    for customer in e.customers:
        assert e.sites_of(customer.id)
        orders = e.orders_of(customer.id)
        assert orders
        assert all(set(o.skus) <= skus for o in orders)


def test_about_the_requested_share_writes_german() -> None:
    e = expand(WORLD, 1, customers=100, german_share=0.3)
    german = sum(c.language == "de" for c in e.customers)
    assert 20 <= german <= 40


def test_emails_and_ids_are_synthetic() -> None:
    e = expand(WORLD, 3)
    assert all(c.email.endswith(".example") for c in e.customers)
    assert all(o.id.startswith("ORD-") for o in e.orders)
