from pathlib import Path

import pytest

from mappd.data.world import WorldError, load

REPO = Path(__file__).parents[2]


def test_the_repository_world_loads() -> None:
    world = load()
    assert len(world.catalog) >= 35
    assert len(world.knowledge_base) >= 18
    assert {a.part for a in world.knowledge_base} == {"technical", "commercial"}
    assert {r.id for r in world.roles} >= {"sales", "billing", "logistics", "presales", "service"}


def test_ids_are_unique_and_articles_ask_a_question() -> None:
    world = load(REPO / "world.yaml")
    skus = [p.sku for p in world.catalog]
    assert len(skus) == len(set(skus))
    ids = [a.id for a in world.knowledge_base]
    assert len(ids) == len(set(ids))
    assert all(a.question.endswith("?") for a in world.knowledge_base)


def test_article_lookup_filters() -> None:
    world = load(REPO / "world.yaml")
    commercial = world.articles(part="commercial", exclude_topics=("refund",))
    assert commercial
    assert all(a.part == "commercial" and a.topic != "refund" for a in commercial)
    assert world.role("service").name


def test_bad_world_names_the_field(tmp_path: Path) -> None:
    bad = tmp_path / "world.yaml"
    bad.write_text((REPO / "world.yaml").read_text().replace("part: technical", "part: secret", 1))
    with pytest.raises(WorldError, match=r"knowledge_base\[\d+\]\.part"):
        load(bad)


def test_duplicate_sku_is_an_error(tmp_path: Path) -> None:
    text = (REPO / "world.yaml").read_text()
    bad = tmp_path / "world.yaml"
    bad.write_text(text.replace("sku: SEN-T100", "sku: CTL-H200", 1))
    with pytest.raises(WorldError, match="duplicate sku CTL-H200"):
        load(bad)


def test_unknown_role_lookup_is_an_error() -> None:
    with pytest.raises(WorldError, match="no role 'ceo'"):
        load(REPO / "world.yaml").role("ceo")


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(WorldError, match="cannot read"):
        load(tmp_path / "absent.yaml")


def test_some_articles_need_a_fact_to_be_answered() -> None:
    world = load(REPO / "world.yaml")
    needing = [a for a in world.knowledge_base if a.needs]
    assert len(needing) >= 6
    assert {n for a in needing for n in a.needs} <= {"order", "site", "device"}
    assert all(not a.needs for a in world.knowledge_base if a.id in {"kb-206", "kb-208", "kb-207"})


def test_device_needs_require_two_named_products(tmp_path: Path) -> None:
    text = (REPO / "world.yaml").read_text()
    bad = tmp_path / "world.yaml"
    question = "    question: Why does my door contact report open when the door is closed?\n"
    bad.write_text(text.replace(question, question + "    needs: [device]\n", 1))
    with pytest.raises(WorldError, match=r"knowledge_base\[11\]\.body"):
        load(bad)


def test_products_in_finds_the_named_products() -> None:
    world = load(REPO / "world.yaml")
    article = next(a for a in world.knowledge_base if a.id == "kb-106")
    names = {p.name for p in world.products_in(article)}
    assert names == {"Network Recorder NVR8", "Network Recorder NVR16"}


def test_the_real_world_has_device_articles_for_clarify() -> None:
    world = load(REPO / "world.yaml")
    device = [a.id for a in world.knowledge_base if "device" in a.needs]
    assert set(device) == {"kb-101", "kb-105", "kb-106", "kb-108"}


def test_only_the_devices_an_answer_is_about_count() -> None:
    world = load(REPO / "world.yaml")
    article = next(a for a in world.knowledge_base if a.id == "kb-105")
    assert {p.family for p in world.products_in(article)} == {"camera"}


def test_the_world_has_installation_sites_depots_slots_and_personas() -> None:
    world = load(REPO / "world.yaml")
    assert len(world.sites) >= 4
    assert all(s.contact.name and s.contact.phone and s.access for s in world.sites)
    assert len(world.depots) >= 2
    assert any(d.stock.get("CTL-H400") == 0 for d in world.depots), "one depot lacks a part"
    assert len(world.delivery_slots) >= 6
    assert {p.id for p in world.personas} >= {"terse", "chatty", "non_native", "late_photos"}
    assert world.site(world.sites[0].name) == world.sites[0]


def test_a_world_without_installation_sections_loads(tmp_path: Path) -> None:
    text = (REPO / "world.yaml").read_text()
    cut = tmp_path / "world.yaml"
    cut.write_text(text.split("\nsites:", 1)[0] + "\n")
    world = load(cut)
    assert world.sites == []
    assert world.depots == []


def test_depot_stock_names_catalog_skus(tmp_path: Path) -> None:
    bad = tmp_path / "world.yaml"
    bad.write_text((REPO / "world.yaml").read_text().replace("CTL-H400: 0", "CTL-NOPE: 0", 1))
    with pytest.raises(WorldError, match="unknown sku CTL-NOPE"):
        load(bad)


def test_a_slot_names_a_known_site(tmp_path: Path) -> None:
    world = load(REPO / "world.yaml")
    site = world.sites[0].name
    bad = tmp_path / "world.yaml"
    bad.write_text((REPO / "world.yaml").read_text().replace(f"site: {site},", "site: Nowhere,", 1))
    with pytest.raises(WorldError, match="unknown site Nowhere"):
        load(bad)
