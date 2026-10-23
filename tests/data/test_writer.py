import json

from mappd.data.cases import CasePlan, Slot
from mappd.data.expand import Customer, Persona
from mappd.data.writer import parse, prompt

CUSTOMER = Customer(
    id="c1-000",
    company="Alder Hotels",
    contact="Mira Keller",
    email="mira.keller@alder.example",
    language="de",
    persona=Persona(
        id="c1-000-terse", style="terse procurement buyer", tone="formal", typo_rate=0.02
    ),
)
PLAN = CasePlan(
    id="s1-0001",
    slot=Slot(index=1, kind="escalate", trap=None),
    customer=CUSTOMER,
    facts=["The customer wants a refund for the Dome Camera D2 from order ORD-12345."],
    labels={"outcome": "escalate", "route": "commercial", "escalate_to": "sales"},
)


def test_prompt_carries_facts_persona_and_language_but_never_labels() -> None:
    text = json.dumps(prompt(PLAN))
    assert "ORD-12345" in text
    assert "terse procurement buyer" in text
    assert "German" in text
    assert "escalate" not in text
    assert "outcome" not in text
    assert "sales" not in text


def test_parse_reads_subject_and_body() -> None:
    message = parse("Subject: Rückerstattung\n\nGuten Tag,\nich möchte ...")
    assert message.subject == "Rückerstattung"
    assert message.body.startswith("Guten Tag")


def test_parse_without_subject_line_keeps_everything_as_body() -> None:
    message = parse("Hello, my sensor is offline.")
    assert message.subject == ""
    assert message.body == "Hello, my sensor is offline."


def test_omissions_become_an_instruction_not_a_fact() -> None:
    plan = PLAN.model_copy(update={"omit": ["the order number"]})
    messages = prompt(plan)
    assert "Do not mention: the order number." in messages[-1]["content"]
    assert "- the order number" not in messages[-1]["content"]


def test_parse_reads_a_german_subject_line() -> None:
    message = parse("Betreff: Lieferdatum ORD-66179\n\nSehr geehrte Damen und Herren,")
    assert message.subject == "Lieferdatum ORD-66179"
    assert message.body == "Sehr geehrte Damen und Herren,"
