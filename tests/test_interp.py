"""Вердикт — по знаковому значению, вывод — по модулю; триггер не отменяет вычисление."""

from decimal import Decimal

import pytest

from dsl import parse
from interp import Ctx, check_trigger, evaluate, verdict


def row(txn, cat, amt, cp="X", desc="d", date="2025-06-01"):
    return {
        "txn_id": txn,
        "cat": cat,
        "amt": Decimal(amt),
        "counterparty": cp,
        "description": desc,
        "date": date,
        "account_id": "ACC-1",
        "currency": "USD",
    }


ROWS = [
    row("T-01", "REVENUE", "1000"),
    row("T-02", "REVENUE", "500", date="2025-11-15"),
    row("T-03", "OTHER_OPEX", "-300"),
    row("T-04", "INTEREST", "-100"),
    row("T-05", "CAPEX", "-200", cp="Ertis Capital LLP"),
    row("T-06", "PAYROLL", "-50", cp="Ertis Capital, LLP"),
]
FACTS = {"related_parties": ["Ertis Capital LLP"], "doc_facts": {"severance_liability": "40"}}
CTX = Ctx(rows=ROWS, facts=FACTS)


def ev(text, ctx=CTX):
    return evaluate(parse(text), ctx)


def test_agg_and_arithmetic():
    assert ev("sub(agg(REVENUE, in), agg(OTHER_OPEX, out))").value == Decimal("1200")
    assert ev("add(agg(PAYROLL, out), doc(severance_liability))").value == Decimal("90")
    assert ev("max(agg(PAYROLL, out), agg(INTEREST, out))").value == Decimal("100")
    assert ev("const(4000000)").value == Decimal("4000000")


def test_filters():
    assert ev("agg(REVENUE, in, quarter(4))").value == Decimal("500")
    assert ev("agg(REVENUE, in, period(2025-01-01, 2025-06-30))").value == Decimal("1000")
    assert ev("agg(ALL, out, counterparty_in(related_parties))").value == Decimal("250")
    assert ev("agg(ALL, out, min_amount(100))").value == Decimal("600")
    assert ev("agg(CAPEX, out, desc_contains('d'))").value == Decimal("200")


def test_set_exclude_rolls_back_inclusion():
    ctx = Ctx(rows=ROWS, facts=FACTS, set_exclude=frozenset({"T-05"}))
    assert evaluate(parse("agg(ALL, out, counterparty_in(related_parties))"), ctx).value == Decimal("50")


def test_ratio_zero_denominator_flagged():
    res = ev("ratio(agg(REVENUE, in), agg(RENT, out))")
    assert res.value == Decimal(0)
    assert "zero_denominator" in res.flags


def test_zero_denominator_max_is_breach():
    # Бесконечное отношение не должно засчитываться как соблюдение потолка.
    from interp import EvalResult

    status, alarms = verdict(EvalResult(Decimal(0), frozenset({"zero_denominator"})), "max", Decimal("3.00"))
    assert status == "BREACH"
    assert "zero_denominator" in alarms


def test_zero_denominator_min_is_compliant():
    # Нулевой знаменатель у min-метрики (ICR без процентных платежей) —
    # покрытие бесконечно: ∞ не меньше порога, подставленный ноль дал бы
    # ложный BREACH (ревью PR #9, 22-я волна).
    from interp import EvalResult

    status, alarms = verdict(EvalResult(Decimal(0), frozenset({"zero_denominator"})), "min", Decimal("1.50"))
    assert status == "COMPLIANT"
    assert "zero_denominator" in alarms


def test_zero_denominator_min_negative_numerator_is_breach():
    # −EBITDA при нулевом знаменателе — это −∞, а не +∞: ложный COMPLIANT
    # недопустим (ревью PR #9, 24-я волна). 0/0 не определён — тоже BREACH.
    rows = [row("T-01", "OTHER_OPEX", "-100")]
    res = evaluate(
        parse("ratio(sub(agg(REVENUE, in), agg(OTHER_OPEX, out)), agg(INTEREST, out))"),
        Ctx(rows, {}),
    )
    assert "zero_denominator" in res.flags and "zero_den_negative_num" in res.flags
    assert verdict(res, "min", Decimal("1.50"))[0] == "BREACH"

    from interp import EvalResult

    zz = EvalResult(Decimal(0), frozenset({"zero_denominator", "zero_den_zero_num"}))
    assert verdict(zz, "min", Decimal("1.50"))[0] == "BREACH"


def test_negative_denominator_min_stays_breach():
    # negative_denominator при min не трогаем: значение действительно
    # отрицательное, вердикт совпадает с истинным.
    from interp import EvalResult

    status, _ = verdict(
        EvalResult(Decimal("-2"), frozenset({"negative_denominator"})), "min", Decimal("1.50")
    )
    assert status == "BREACH"


def test_negative_denominator_max_is_breach():
    rows = [
        row("T-01", "REVENUE", "100"),
        row("T-02", "OTHER_OPEX", "-280"),
        row("T-03", "CAPEX", "-1700"),
    ]
    res = evaluate(
        parse("ratio(agg(CAPEX, out), sub(agg(REVENUE, in), agg(OTHER_OPEX, out)))"),
        Ctx(rows, {}),
    )
    assert res.value < 0 and "negative_denominator" in res.flags
    status, alarms = verdict(res, "max", Decimal("9.00"))
    assert status == "BREACH"  # −9.44 при max 9.00: не COMPLIANT
    assert "negative_denominator" in alarms


def test_signed_verdict():
    from interp import EvalResult

    assert verdict(EvalResult(Decimal("10"), frozenset()), "max", Decimal("9"))[0] == "BREACH"
    assert verdict(EvalResult(Decimal("8"), frozenset()), "max", Decimal("9"))[0] == "COMPLIANT"
    assert verdict(EvalResult(Decimal("1"), frozenset()), "min", Decimal("2"))[0] == "BREACH"


@pytest.mark.parametrize(
    ("trig", "want"),
    [
        ("gt(agg(REVENUE, in), const(1000))", True),
        ("gt(agg(REVENUE, in), const(2000))", False),
        ("le(agg(RENT, out), const(0))", True),
    ],
)
def test_trigger(trig, want):
    assert check_trigger(parse(trig), CTX) is want


def test_trigger_none_means_always_applies():
    assert check_trigger(None, CTX) is True
