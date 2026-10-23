from mappd.data.roundtrip import compare, parse_fields

FIELDS = ("topic", "route", "urgency", "language", "outcome")
STRICT = ("route", "outcome", "language")
LABELS = {
    "topic": "delivery",
    "route": "commercial",
    "urgency": "normal",
    "language": "de",
    "outcome": "reply",
}


def test_agreement_keeps_the_case() -> None:
    assert compare(LABELS, dict(LABELS), FIELDS, STRICT).keep


def test_strict_disagreement_drops_and_names_the_field() -> None:
    verdict = compare(LABELS, {**LABELS, "outcome": "escalate"}, FIELDS, STRICT)
    assert not verdict.keep
    assert verdict.disagreements == ["outcome"]


def test_loose_disagreement_is_counted_but_kept() -> None:
    verdict = compare(LABELS, {**LABELS, "urgency": "high"}, FIELDS, STRICT)
    assert verdict.keep
    assert verdict.disagreements == ["urgency"]


def test_missing_strict_field_drops() -> None:
    got = {k: v for k, v in LABELS.items() if k != "language"}
    assert not compare(LABELS, got, FIELDS, STRICT).keep


def test_parse_fields_accepts_fenced_json_and_rejects_garbage() -> None:
    assert parse_fields('```json\n{"route": "technical"}\n```') == {"route": "technical"}
    assert parse_fields("no json here") == {}
