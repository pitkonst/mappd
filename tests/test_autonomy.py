from typing import TYPE_CHECKING, Literal

import pytest
from pydantic import BaseModel

from mappd.autonomy import PolicyError, categories, load, set_level, set_shadow

if TYPE_CHECKING:
    from pathlib import Path

POLICY = """\
# Reviewed like code.
shadow: false          # true: nothing is sent
default: review
categories:
  how_to: review       # FAQ-like
  malfunction:   review
ready:
  without_edits: 0.9
  false_escalations_max: 0.1
  min_cases: 30
"""
ALLOWED = ("how_to", "malfunction", "billing", "other")


@pytest.fixture
def policy(tmp_path: Path) -> Path:
    path = tmp_path / "autonomy.yaml"
    path.write_text(POLICY)
    return path


def test_load_reads_levels_and_defaults(policy: Path) -> None:
    p = load(policy)
    assert p.shadow is False
    assert p.level("how_to") == "review"
    assert p.level("billing") == "review", "unlisted falls back to default"
    assert p.ready.min_cases == 30


def test_missing_file_is_the_safe_default(tmp_path: Path) -> None:
    p = load(tmp_path / "nope.yaml")
    assert (p.shadow, p.default, p.categories) == (False, "review", {})


@pytest.mark.parametrize(
    ("text", "field"),
    [("shadow: maybe\n", "shadow"), ("categories:\n  how_to: sometimes\n", "categories.how_to")],
)
def test_malformed_file_names_file_and_field(tmp_path: Path, text: str, field: str) -> None:
    path = tmp_path / "autonomy.yaml"
    path.write_text(text)
    with pytest.raises(PolicyError, match=f"autonomy.yaml.*{field}"):
        load(path)


def test_set_shadow_rewrites_one_line_and_keeps_its_comment(policy: Path) -> None:
    diff = set_shadow(policy, True)
    assert "-shadow: false" in diff
    assert "+shadow: true" in diff
    assert "shadow: true          # true: nothing is sent" in policy.read_text()
    assert load(policy).shadow is True


def test_set_level_inserts_missing_and_keeps_comments(policy: Path) -> None:
    set_level(policy, "how_to", "auto", allowed=ALLOWED)
    set_level(policy, "billing", "auto", allowed=ALLOWED)
    text = policy.read_text()
    assert "  how_to: auto       # FAQ-like" in text
    assert "# Reviewed like code." in text
    p = load(policy)
    assert (p.level("how_to"), p.level("billing"), p.level("malfunction")) == (
        "auto",
        "auto",
        "review",
    )


def test_set_is_idempotent(policy: Path) -> None:
    assert set_level(policy, "malfunction", "review", allowed=ALLOWED) == ""
    assert set_shadow(policy, False) == ""
    assert policy.read_text() == POLICY


def test_set_level_refuses_unknown_categories(policy: Path) -> None:
    with pytest.raises(ValueError, match="refund"):
        set_level(policy, "refund", "auto", allowed=ALLOWED)


def test_set_creates_the_file_when_missing(tmp_path: Path) -> None:
    path = tmp_path / "autonomy.yaml"
    set_level(path, "how_to", "auto", allowed=ALLOWED)
    assert load(path).level("how_to") == "auto"


def test_categories_come_from_a_literal_field() -> None:
    type Topic = Literal["how_to", "refund"]

    class Out(BaseModel):
        topic: Topic

    assert categories(Out, "topic") == ("how_to", "refund")


@pytest.mark.parametrize(
    "text",
    [
        "shadow: false\ncategories:",  # section last, no trailing newline
        "shadow: false",  # no section, no trailing newline
        "categories:\n  how_to: review\n# billing is new\n  billing: review\n",  # column-0 comment
        "categories:\n    how_to: review\n",  # 4-space children
        "categories: {}\n",  # flow style
    ],
)
def test_set_level_leaves_a_valid_policy_with_the_level_set(tmp_path: Path, text: str) -> None:
    path = tmp_path / "autonomy.yaml"
    path.write_text(text)
    try:
        set_level(path, "billing", "auto", allowed=ALLOWED)
    except PolicyError:
        assert path.read_text() == text, "a refused edit leaves the file alone"
        return
    p = load(path)
    assert p.level("billing") == "auto"


def test_misspelled_keys_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "autonomy.yaml"
    path.write_text("shadwo: true\n")
    with pytest.raises(PolicyError, match="shadwo"):
        load(path)
    path.write_text("ready:\n  min_case: 3\n")
    with pytest.raises(PolicyError, match="min_case"):
        load(path)
