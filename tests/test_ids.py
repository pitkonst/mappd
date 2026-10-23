from uuid import UUID

import pytest

from mappd.ids import decision, from_key, mail_case


def test_from_key_joins_kind_and_key() -> None:
    assert from_key("ticket", "m-1") == "ticket:m-1"


def test_from_key_accepts_uuid() -> None:
    u = UUID("12345678-1234-5678-1234-567812345678")
    assert from_key("case", u) == "case:12345678-1234-5678-1234-567812345678"


def test_decision_appends_the_version() -> None:
    assert decision("approve", "tg-777", 3) == "approve:tg-777:v3"


def test_colons_and_percent_in_keys_are_encoded() -> None:
    # "<a:b@x>" must not be confused with kind "a" + key "b@x", nor collide with another split.
    assert from_key("mail", "<a:b@x>") == "mail:<a%3Ab@x>"
    assert from_key("mail", "100%") == "mail:100%25"
    assert decision("approve", "a:v1", 2) != decision("approve", "a", 1)


@pytest.mark.parametrize("key", [7, True, 3.0, None, b"bytes"])
def test_counters_and_other_types_are_rejected(key: object) -> None:
    with pytest.raises(TypeError, match="str or UUID"):
        from_key("ticket", key)  # type: ignore[arg-type]


def test_empty_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="key must not be empty"):
        from_key("ticket", "")


@pytest.mark.parametrize("kind", ["", "Ticket", "1st", "a:b", "with space"])
def test_kind_must_be_a_lowercase_name(kind: str) -> None:
    with pytest.raises(ValueError, match="kind"):
        from_key(kind, "k")


@pytest.mark.parametrize("version", [-1, True, 3.0])
def test_version_must_be_a_non_negative_int(version: object) -> None:
    with pytest.raises(ValueError, match="version"):
        decision("approve", "k", version)  # type: ignore[arg-type]


def test_a_mail_case_key_is_stable_and_safe_in_urls() -> None:
    key = mail_case("<CAH+x/y?z#1@mail.example.com>")
    assert key == mail_case("<CAH+x/y?z#1@mail.example.com>")
    assert key != mail_case("<other@mail.example.com>")
    assert key.startswith("mail-")
    assert len(key) == len("mail-") + 16
    assert key[5:].isalnum()
    with pytest.raises(ValueError, match="message_id"):
        mail_case("")
