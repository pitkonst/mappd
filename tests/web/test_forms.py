from enum import Enum
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from mappd.web import FormError, form, parse_form

type Team = Literal["sales", "service"]


class Colour(Enum):
    RED = "red"
    BLUE = "blue"


class Address(BaseModel):
    city: str
    zip_code: str | None = None


class Everything(BaseModel):
    name: str = Field(description="Who asks")
    note: str = Field(default="", json_schema_extra={"widget": "textarea"})
    count: int
    ratio: float = 0.5
    urgent: bool = False
    team: Team | None = None
    colour: Colour = Colour.RED
    tags: list[str] = []
    address: Address
    version: int = 0


def test_renders_every_supported_type() -> None:
    html = str(form(Everything, {"name": "Anna", "count": 2, "address": {"city": "Riga"}}))
    assert '<input type="text" id="f-name" name="name" value="Anna"' in html
    assert "Who asks" in html
    assert '<textarea id="f-note" name="note"' in html
    assert '<input type="number" id="f-count" name="count" step="1" value="2"' in html
    assert 'step="any"' in html
    assert '<input type="checkbox" id="f-urgent" name="urgent" value="true"' in html
    assert '<select id="f-colour" name="colour">' in html
    assert '<option value="red" selected>' in html
    assert '<textarea id="f-tags" name="tags"' in html
    assert "<fieldset><legend>address</legend>" in html
    assert 'name="address.city" value="Riga"' in html


def test_type_alias_literal_renders_a_select() -> None:
    html = str(form(Everything, {"team": "service"}))
    assert '<select id="f-team" name="team">' in html
    assert '<option value=""></option>' in html, "optional select offers an empty choice"
    assert '<option value="service" selected>' in html


def test_values_are_escaped() -> None:
    html = str(form(Everything, {"name": '"><script>x</script>'}))
    assert "<script>" not in html


def test_hidden_fields_render_as_hidden_inputs() -> None:
    html = str(form(Everything, {"version": 3}, hidden={"version"}))
    assert '<input type="hidden" name="version" value="3">' in html
    assert 'id="f-version"' not in html


def test_errors_show_next_to_their_field() -> None:
    html = str(form(Everything, errors={"address.city": "Field required"}))
    assert '<span class="error">Field required</span>' in html


def test_values_may_be_a_model_instance() -> None:
    value = Address(city="Riga")
    assert 'value="Riga"' in str(form(Address, value))


def test_unsupported_type_raises_naming_the_field() -> None:
    class Bad(BaseModel):
        blob: dict[str, int]

    with pytest.raises(TypeError, match="blob"):
        form(Bad)


def test_parse_round_trip() -> None:
    data = {
        "name": "Anna",
        "note": "",
        "count": "2",
        "ratio": "0.25",
        "urgent": "true",
        "team": "",
        "colour": "blue",
        "tags": "a\n\n b \n",
        "address.city": "Riga",
        "address.zip_code": "",
        "version": "4",
    }
    parsed = parse_form(Everything, data)
    assert parsed.count == 2
    assert parsed.ratio == 0.25
    assert parsed.urgent is True
    assert parsed.team is None
    assert parsed.colour is Colour.BLUE
    assert parsed.tags == ["a", "b"]
    assert parsed.address == Address(city="Riga", zip_code=None)
    assert parsed.version == 4


def test_unchecked_checkbox_is_false() -> None:
    parsed = parse_form(Everything, {"name": "a", "count": "1", "address.city": "x"})
    assert parsed.urgent is False


def test_parse_errors_carry_dotted_paths() -> None:
    with pytest.raises(FormError) as exc:
        parse_form(Everything, {"name": "a", "count": "two"})
    assert set(exc.value.errors) == {"count", "address.city"}


def test_multi_valued_input_takes_the_first() -> None:
    assert parse_form(Address, {"city": ["Riga", "Oslo"]}).city == "Riga"


def test_an_error_on_a_hidden_field_is_shown() -> None:
    html = str(
        form(
            Everything,
            {"version": "x"},
            errors={"version": "Input should be an int"},
            hidden={"version"},
        )
    )
    assert '<input type="hidden" name="version" value="x">' in html
    assert '<span class="error">version: Input should be an int</span>' in html


def test_two_forms_on_a_page_get_their_own_ids() -> None:
    first = str(form(Address, id_prefix="approve"))
    second = str(form(Address, id_prefix="escalate"))
    assert '<label for="approve-city">' in first
    assert 'id="approve-city" name="city"' in first
    assert 'id="escalate-city" name="city"' in second
    assert 'id="approve-address' not in first, "nested fields keep the prefix too"
    nested = str(form(Everything, {"address": {"city": "Riga"}}, id_prefix="x"))
    assert 'id="x-address.city"' in nested
