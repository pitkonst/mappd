from typing import Any

from mappd.app import create_app
from mappd.web.openapi import REF, short_name, tidy


def refs(node: Any) -> set[str]:
    if isinstance(node, dict):
        found = {node["$ref"]} if isinstance(node.get("$ref"), str) else set()
        return found.union(*(refs(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(refs(v) for v in node))
    return set()


def test_short_names_are_owner_and_model() -> None:
    assert short_name("processes__rfq__schemas__Decision") == "RfqDecision"
    assert short_name("mappd__web__workflows_api__Status") == "WorkflowsApiStatus"
    assert short_name("Decision") == "Decision"


def test_a_short_name_never_collides() -> None:
    schema: dict[str, Any] = {
        "components": {"schemas": {"RfqDecision": {}, "processes__rfq__schemas__Decision": {}}}
    }
    assert set(tidy(schema)["components"]["schemas"]) == {
        "RfqDecision",
        "processes__rfq__schemas__Decision",
    }


def test_the_app_schema_has_no_module_paths_and_no_dangling_refs() -> None:
    schema = create_app().openapi()
    names = set(schema["components"]["schemas"])
    assert not [n for n in names if "__" in n]
    assert {"RfqDecision", "SupportDecision", "InstallationDecision"} <= names
    assert {r.removeprefix(REF) for r in refs(schema)} <= names
