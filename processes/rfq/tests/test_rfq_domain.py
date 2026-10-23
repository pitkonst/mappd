from decimal import Decimal
from typing import Literal

import pytest

from processes.rfq import catalog, domain
from processes.rfq.schemas import Extracted, Extraction, Line


def ext(*lines: Extracted, needed_by: int | None = None, custom: bool = False) -> Extraction:
    return Extraction(
        lines=list(lines), needed_by_days=needed_by, custom_request=custom, language="en"
    )


def line(sku: str | None, qty: int | None, conf: Literal["low", "high"] = "high") -> Extracted:
    return Extracted(requested=sku or "thing", quantity=qty, sku=sku, confidence=conf)


def test_search_finds_by_words_in_the_name() -> None:
    hits = catalog.search("co2 sensor")
    assert hits[0].sku == "SEN-C300"


def test_prices_and_totals_come_from_the_catalog() -> None:
    out = domain.solve_output(ext(line("SEN-T100", 12), line("CTL-H400", 1)))
    assert [(ln.sku, ln.unit_price, ln.line_total) for ln in out.lines] == [
        ("SEN-T100", Decimal("48.00"), Decimal("576.00")),
        ("CTL-H400", Decimal("690.00"), Decimal("690.00")),
    ]
    assert out.total == Decimal("1266.00")
    assert out.outcome == "quote"
    assert out.draft is not None
    assert "1266.00" in out.draft


def test_gate_at_the_threshold() -> None:
    # CTL-H800 costs 1150.00: 8 → 9200 quote; 9 → 10350 escalate to sales
    assert domain.solve_output(ext(line("CTL-H800", 8))).outcome == "quote"
    big = domain.solve_output(ext(line("CTL-H800", 9)))
    assert (big.outcome, big.escalate_to) == ("escalate", "sales")
    exact = [
        Line(
            requested="x",
            quantity=1,
            sku="X",
            name="x",
            unit_price=Decimal("10000.00"),
            line_total=Decimal("10000.00"),
            lead_days=1,
            alternative=None,
            late=False,
        )
    ]
    assert domain.decide(exact, ext())[0] == "quote", "exactly the threshold still quotes"


def test_custom_requests_go_to_presales_first() -> None:
    out = domain.solve_output(ext(line(None, None), custom=True))
    assert (out.outcome, out.escalate_to, out.draft) == ("escalate", "presales", None)


@pytest.mark.parametrize(
    "bad",
    [line("NOPE-1", 2), line("SEN-T100", None), line("SEN-T100", 0), line("SEN-T100", 3, "low")],
)
def test_unknown_sku_and_bad_quantity_clarify(bad: Extracted) -> None:
    out = domain.solve_output(ext(line("SEN-T100", 2), bad))
    assert out.outcome == "clarify"
    assert out.draft is not None
    assert bad.requested in out.draft
    assert all(ln.sku is None or ln.sku in catalog.PRODUCTS for ln in out.lines)


def test_alternative_for_a_late_line() -> None:
    # CAM-PTZ: 10 days. Needed in 3 days → same-family camera with lead ≤ 3, closest price.
    out = domain.solve_output(ext(line("CAM-PTZ", 1), needed_by=3))
    [ln] = out.lines
    assert ln.alternative == "CAM-B6"  # 330.00, the closest-priced camera arriving in 3 days
    assert ln.late is True, "late means late; the alternative is its own field"
    assert out.draft is not None
    assert "late: 10 days" in out.draft, "the draft says the original misses the deadline"


def test_late_without_alternative() -> None:
    # Floor cabinets take 21 days; no cabinet arrives within 1 day.
    out = domain.solve_output(ext(line("CAB-F120", 1), needed_by=1))
    [ln] = out.lines
    assert (ln.alternative, ln.late) == (None, True)
    assert out.outcome == "quote"
    assert out.draft is not None
    assert "late" in out.draft.lower()


def test_german_draft() -> None:
    e = Extraction(
        lines=[line("SEN-T100", 1)], needed_by_days=None, custom_request=False, language="de"
    )
    draft = domain.solve_output(e).draft or ""
    assert "Angebot" in draft
    # The line once read "at 48.00" inside a German quote.
    assert "(SEN-T100) à 48.00 = 48.00 EUR" in draft
    assert " at " not in draft


def test_a_reply_keeps_every_figure_line_of_the_quote() -> None:
    quote = domain.solve_output(ext(line("SEN-T100", 12)))
    assert quote.draft is not None
    figures = domain.figures(quote)
    assert figures == [
        "12 x Temperature Sensor T100 (SEN-T100) at 48.00 = 576.00 EUR",
        "Total: 576.00 EUR (excl. VAT)",
    ]
    assert domain.reply_body(quote, quote, None) == quote.draft
    added = f"Dear Anna,\n\n{quote.draft}\n\nKind regards"
    assert domain.reply_body(quote, quote, added) == added
    with pytest.raises(ValueError, match="through lines"):
        domain.reply_body(quote, quote, quote.draft.replace("576.00", "25.00"))


def test_a_question_may_be_reworded() -> None:
    question = domain.solve_output(ext(line(None, 3)))
    assert question.outcome == "clarify"
    assert domain.figures(question) == []
    assert domain.reply_body(question, question, "Which sensor?") == "Which sensor?"


@pytest.mark.parametrize(
    "added",
    [
        "Special deal: 50% off the whole order, final price 1102.50 EUR.",
        "Final price 1102.50.",
        "Only € 900 for you.",
        "We knock off ten percent.",
        "Price in USD on request.",
    ],
)
def test_added_text_may_not_name_its_own_price(added: str) -> None:
    quote = domain.solve_output(ext(line("SEN-T100", 12)))
    with pytest.raises(ValueError, match="amount, a currency or a percentage"):
        domain.reply_body(quote, quote, f"{quote.draft}\n\n{added}")


@pytest.mark.parametrize(
    "added", ["Delivery to Hall 2 on 12.11. as agreed.", "Order ORD-52767, 3 pallets."]
)
def test_added_text_without_prices_goes_out(added: str) -> None:
    quote = domain.solve_output(ext(line("SEN-T100", 12)))
    reply = f"Dear Anna,\n\n{quote.draft}\n\n{added}"
    assert domain.reply_body(quote, quote, reply) == reply


def test_an_escalation_says_why() -> None:
    big = domain.solve_output(ext(line("SEN-T100", 500)))
    assert big.outcome == "escalate"
    assert "above 10000" in (domain.escalation_reason(big) or "")
    assert domain.escalated_by_rule(big) is not None, "the threshold is a rule"
    custom = domain.solve_output(ext(line("SEN-T100", 1), custom=True))
    assert "custom work" in (domain.escalation_reason(custom) or "")
    assert domain.escalated_by_rule(custom) is None, "custom work is the agent's reading"
    assert domain.escalation_reason(domain.solve_output(ext(line("SEN-T100", 1)))) is None
