from mappd.data.cases import Case
from processes.rfq.evals import metrics


def case(**expected: object) -> Case:
    return Case(id="c", input={}, expected=expected, meta={})


def test_line_items_f1_counts_pairs_and_ignores_unmatched() -> None:
    c = case(lines=[{"sku": "A", "quantity": 2}, {"sku": "B", "quantity": 1}])
    out = {
        "lines": [
            {"sku": "A", "quantity": 2},
            {"sku": "B", "quantity": 3},
            {"sku": None, "quantity": 1},
        ]
    }
    assert metrics.line_items_f1(c, out) == 0.5
    assert metrics.sku_f1(c, out) == 1.0


def test_empty_lines_on_both_sides_score_one() -> None:
    assert metrics.line_items_f1(case(lines=[]), {"lines": []}) == 1.0


def test_duplicate_skus_are_a_multiset() -> None:
    c = case(lines=[{"sku": "A", "quantity": 1}, {"sku": "A", "quantity": 1}])
    assert metrics.line_items_f1(c, {"lines": [{"sku": "A", "quantity": 1}]}) == 2 / 3


def test_totals_check_parses_stored_decimals() -> None:
    c = case(outcome="quote", total="1266.00")
    assert metrics.totals_check(c, {"total": "1266.00"}) == 1.0
    assert metrics.totals_check(c, {"total": 1266.0}) == 1.0
    assert metrics.totals_check(c, {"total": "1266.01"}) == 0.0
    assert metrics.totals_check(case(outcome="clarify", total="0"), {"total": "0"}) is None


def test_alternatives_accuracy() -> None:
    c = case(alternatives={"CAM-PTZ": "CAM-B6"})
    assert (
        metrics.alternatives_accuracy(c, {"lines": [{"sku": "CAM-PTZ", "alternative": "CAM-B6"}]})
        == 1.0
    )
    assert (
        metrics.alternatives_accuracy(c, {"lines": [{"sku": "CAM-PTZ", "alternative": None}]})
        == 0.0
    )
    assert metrics.alternatives_accuracy(case(alternatives={}), {"lines": []}) is None
