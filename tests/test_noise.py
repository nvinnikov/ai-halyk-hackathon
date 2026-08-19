"""Диагностика шума леджера: признак загрязнённости и алярм на сочетание."""

from decimal import Decimal

import noise
from dsl import parse


def _rows(amounts, cat="PAYROLL"):
    return [
        {
            "txn_id": f"TXN-A-{i:04d}",
            "amt": Decimal(str(a)),
            "cat": cat,
            "counterparty": f"Vendor {i} LLP",
            "description": "",
            "date": "2025-03-01",
            "account_id": "ACC-0001",
        }
        for i, a in enumerate(amounts)
    ]


def test_pollution_ratio_of_a_clean_account_is_small():
    assert noise.pollution_ratio(_rows([-100, -120, -90, -800, -110])) < noise.POLLUTION_LEVEL


def test_pollution_ratio_sees_a_planted_outlier():
    assert noise.pollution_ratio(_rows([-100, -120, -90, -110, -300_000_000])) >= noise.POLLUTION_LEVEL


def test_pollution_ratio_is_zero_without_amounts():
    assert noise.pollution_ratio([]) == Decimal(0)
    assert noise.pollution_ratio(_rows([0, 0])) == Decimal(0)


def test_no_alarm_when_the_account_is_polluted_but_the_category_is_narrow():
    ratio = noise.pollution_ratio(_rows([-100, -120, -300_000_000]))
    assert noise.rollup_alarm("ACC-0001", ratio, parse("agg(CAPEX, out)")) is None


def test_no_alarm_when_the_account_is_clean_but_the_metric_reads_a_rollup():
    ratio = noise.pollution_ratio(_rows([-100, -120, -90]))
    assert noise.rollup_alarm("ACC-0001", ratio, parse("agg(OPEX_TOTAL, out)")) is None


def test_alarm_only_on_the_combination():
    ratio = noise.pollution_ratio(_rows([-100, -120, -300_000_000]))
    got = noise.rollup_alarm("ACC-0001", ratio, parse("agg(OPEX_TOTAL, out)"))
    assert got is not None
    assert got["kind"] == "polluted_rollup_read"
    assert got["categories"] == ["OPEX_TOTAL"]


def test_rollup_narrowed_by_counterparty_is_not_a_wide_read():
    """Набор связанных сторон отсекает шум сам: широким чтением это не считается."""
    ratio = noise.pollution_ratio(_rows([-100, -120, -300_000_000]))
    node = parse("agg(ALL, out, counterparty_in(related_parties))")
    assert noise.rollup_alarm("ACC-0001", ratio, node) is None


def test_wide_rollup_reads_are_sorted_and_deduplicated():
    node = parse("ratio(agg(OPEX_TOTAL, out), sub(agg(ALL, in), agg(OPEX_TOTAL, in)))")
    assert noise.wide_rollup_reads(node) == ["ALL", "OPEX_TOTAL"]


# --- вырожденные входы: диагностика не имеет права бросить -------------------


def test_rows_without_an_amount_are_skipped_not_fatal():
    rows = _rows([-100, -200])
    rows[0].pop("amt")
    rows[1]["amt"] = None
    assert noise.pollution_ratio(rows) == Decimal(0)


def test_non_numeric_amount_falls_out_of_the_sample():
    rows = _rows([-100, -120, -300_000_000])
    rows[0]["amt"] = "не число"
    rows[1]["amt"] = True  # bool — не сумма, хотя формально int
    assert noise.pollution_ratio(rows) == Decimal(1)


def test_integer_amounts_are_accepted_as_decimal():
    rows = _rows([-100, -120, -300_000_000])
    for r in rows:
        r["amt"] = int(r["amt"])
    assert noise.pollution_ratio(rows) >= noise.POLLUTION_LEVEL


def test_metric_without_aggregates_never_alarms():
    assert noise.wide_rollup_reads(parse("const(5)")) == []
    assert noise.rollup_alarm("ACC-0001", Decimal(1000), parse("const(5)")) is None


def test_missing_metric_is_not_fatal():
    assert noise.wide_rollup_reads(None) == []
    assert noise.rollup_alarm("ACC-0001", Decimal(1000), None) is None
