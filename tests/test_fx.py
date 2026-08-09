"""Направление в имени поля; конфликты детерминированы; 1.0 — не ступень лестницы."""

from decimal import Decimal

from engine import prepare_rows
from fx import coverage_alarms, pick_rate, to_usd


def rate(cur="EUR", usd="1.16", frm="2025-01-01", to="2025-12-31", ddate="", dhash="", quote="q"):
    return {
        "currency": cur,
        "usd_per_unit": usd,
        "effective_from": frm,
        "effective_to": to,
        "source_quote": quote,
        "derivation": "table",
        "doc_date": ddate,
        "doc_hash": dhash,
    }


def row(txn, amt, cur, date="2025-06-01"):
    return {
        "txn_id": txn,
        "amt": None if amt is None else Decimal(amt),
        "currency": cur,
        "date": date,
        "cat": "TAX",
        "account_id": "ACC-1",
        "counterparty": "X",
        "description": "d",
    }


def test_multiply_by_usd_per_unit():
    rows, alarms = to_usd([row("T-1", "-100", "EUR")], [rate(usd="1.16")], [])
    assert rows[0]["amt"] == Decimal("-116.00")
    assert rows[0]["currency"] == "USD"
    assert alarms == []


def test_usd_rows_untouched():
    rows, _ = to_usd([row("T-1", "-100", "USD")], [], [])
    assert rows[0]["amt"] == Decimal("-100")


def test_period_respected():
    rates = [
        rate(usd="1.10", frm="2025-01-01", to="2025-06-30"),
        rate(usd="1.20", frm="2025-07-01", to="2025-12-31"),
    ]
    rows, _ = to_usd([row("T-1", "-100", "EUR", date="2025-08-01")], rates, [])
    assert rows[0]["amt"] == Decimal("-120.00")


def test_conflict_resolved_deterministically_and_flagged():
    rates = [
        rate(usd="1.10", ddate="2025-05-01", dhash="bb"),
        rate(usd="1.20", ddate="2025-05-01", dhash="aa"),
        rate(usd="1.30", ddate="2025-04-01", dhash="cc"),
    ]
    picked = pick_rate(rates, "EUR", "2025-06-01")
    # последняя дата документа; при равных — возрастание хеша
    assert picked["usd_per_unit"] == "1.20"
    assert picked["conflict"] is True
    # порядок кандидатов в списке не влияет на выбор
    assert pick_rate(list(reversed(rates)), "EUR", "2025-06-01")["usd_per_unit"] == "1.20"


def test_conflict_alarm_carries_all_candidates():
    """Человеку в окне 9 августа нужен весь список, а не только выбранное значение."""
    rates = [
        rate(usd="1.10", ddate="2025-05-01", dhash="bb", quote="служебная записка"),
        rate(usd="1.20", ddate="2025-05-01", dhash="aa", quote="таблица курсов"),
    ]
    _, alarms = to_usd([row("T-1", "-100", "EUR")], rates, [])
    conflict = [a for a in alarms if a["kind"] == "fx_conflict"]
    assert len(conflict) == 1
    cands = conflict[0]["candidates"]
    assert [c["usd_per_unit"] for c in cands] == ["1.20", "1.10"]  # в порядке разрешения
    assert [c["source_quote"] for c in cands] == ["таблица курсов", "служебная записка"]


def test_same_value_from_two_documents_is_not_a_conflict():
    rates = [rate(usd="1.16", ddate="2025-05-01", dhash="aa"), rate(usd="1.1600", ddate="2025-04-01")]
    assert pick_rate(rates, "EUR", "2025-06-01")["conflict"] is False


def test_donor_ladder_no_silent_one():
    rows, alarms = to_usd([row("T-1", "-100", "EUR")], [], [rate(usd="1.16")])
    assert rows[0]["amt"] == Decimal("-116.00")
    assert any(a["kind"] == "fx_donor_used" for a in alarms)


def test_own_rate_wins_over_donor():
    rows, alarms = to_usd([row("T-1", "-100", "EUR")], [rate(usd="1.10")], [rate(usd="1.90")])
    assert rows[0]["amt"] == Decimal("-110.00")
    assert not any(a["kind"] == "fx_donor_used" for a in alarms)


def test_uncovered_row_excluded_with_alarm():
    rows, alarms = to_usd([row("T-1", "-100", "KZT")], [rate()], [])
    assert rows == []
    assert any(a["kind"] == "fx_uncovered_row" for a in alarms)


def test_rate_out_of_period_falls_to_nearest_with_alarm():
    """Интервал дату не накрыл — работает ступень ближайшего по дате курса:
    дрейф курса за дни — малая ошибка, выброс строки — уверенная ошибка на
    весь её вес. Молчаливого растяжения нет: алярм с расстоянием обязателен."""
    rows, alarms = to_usd(
        [row("T-1", "-100", "EUR", date="2025-08-01")],
        [rate(frm="2025-01-01", to="2025-06-30")],
        [],
    )
    assert rows[0]["amt"] == Decimal("-116.00")
    nearest = [a for a in alarms if a["kind"] == "fx_nearest_used"]
    assert nearest and nearest[0]["distance_days"] == 32


def test_nearest_picks_minimal_distance_from_common_pool():
    # Донорский курс ближе своего — расстояние важнее источника.
    rows, alarms = to_usd(
        [row("T-1", "-100", "EUR", date="2025-08-01")],
        [rate(usd="1.10", frm="2025-01-01", to="2025-03-31")],
        [rate(usd="1.20", frm="2025-08-10", to="2025-12-31")],
    )
    assert rows[0]["amt"] == Decimal("-120.00")
    assert [a["kind"] for a in alarms] == ["fx_nearest_used"]


def test_nearest_is_deterministic_on_tie():
    # Равное расстояние с двух сторон: побеждает поздний doc_date, при равных
    # — меньший хеш; выбор не зависит от порядка на входе.
    a = rate(usd="1.10", frm="2025-01-01", to="2025-07-01", ddate="2025-05-01", dhash="bb")
    b = rate(usd="1.20", frm="2025-09-01", to="2025-12-31", ddate="2025-05-01", dhash="aa")
    for rates in ([a, b], [b, a]):
        rows, _ = to_usd([row("T-1", "-100", "EUR", date="2025-08-01")], list(rates), [])
        assert rows[0]["amt"] == Decimal("-120.00")


def test_coverage_check_before_compute():
    alarms = coverage_alarms([row("T-1", "-1", "KZT")], [], [])
    assert alarms and alarms[0]["kind"] == "fx_uncovered"


def test_coverage_silent_when_nearest_covers():
    # Согласованность с лестницей to_usd: пара, которую вытянет ступень
    # ближайшего курса, не считается непокрытой.
    alarms = coverage_alarms(
        [row("T-1", "-1", "EUR", date="2025-08-01")],
        [rate(frm="2025-01-01", to="2025-06-30")],
        [],
    )
    assert alarms == []


def test_coverage_silent_when_donor_covers():
    assert coverage_alarms([row("T-1", "-1", "EUR")], [], [rate()]) == []


def test_unbounded_interval_flagged():
    """Курс без интервала применяется, но помечается — требование трейса 5.5.1."""
    rows, alarms = to_usd([row("T-1", "-100", "EUR")], [rate(frm="", to="")], [])
    assert rows[0]["amt"] == Decimal("-116.00")
    assert rows[0]["fx_unbounded_interval"] is True
    unbounded = [a for a in alarms if a["kind"] == "fx_unbounded_interval"]
    assert unbounded and unbounded[0]["txn"] == "T-1"


def test_bounded_interval_not_flagged():
    rows, alarms = to_usd([row("T-1", "-100", "EUR")], [rate()], [])
    assert "fx_unbounded_interval" not in rows[0]
    assert alarms == []


def test_intermediate_sum_keeps_full_precision():
    """Округление — только на выводе (util.q2); стадия fx копейки не режет."""
    rows, _ = to_usd([row("T-1", "-100.01", "EUR")], [rate(usd="1.159876543")], [])
    assert rows[0]["amt"] == Decimal("-100.01") * Decimal("1.159876543")


def test_rows_without_amount_pass_through_but_are_flagged():
    """Сумму такой строке вернёт amount_override — он уже в USD, конвертировать
    нечего. Но именно здесь допущение о валюте override несущее — нужен алярм."""
    rows, alarms = to_usd([row("T-1", None, "EUR")], [], [])
    assert rows[0]["amt"] is None
    assert [a["kind"] for a in alarms] == ["fx_missing_amount_non_usd"]
    # покрытие такой строки не требуется: конвертировать нечего
    assert coverage_alarms([row("T-1", None, "EUR")], [], []) == []


def test_unparsable_rate_is_not_a_rate():
    """Мусор из извлечения не роняет сценарий и не превращается в 1.0."""
    rows, alarms = to_usd([row("T-1", "-100", "EUR")], [rate(usd="н/д")], [])
    assert rows == []
    assert [a["kind"] for a in alarms] == ["fx_uncovered_row"]


def test_non_positive_rate_is_not_a_rate():
    """Курс '0' обнулил бы строку молча — он выбывает, и работает лестница."""
    rows, alarms = to_usd([row("T-1", "-100", "EUR")], [rate(usd="0")], [rate(usd="1.16")])
    assert rows[0]["amt"] == Decimal("-116.00")
    assert any(a["kind"] == "fx_donor_used" for a in alarms)


def test_amount_override_is_in_usd_and_not_reconverted():
    """Записка казначейства фиксирует итоговую долларовую сумму: override
    применяется ПОСЛЕ fx и курсом не домножается."""
    raw, _ = to_usd([row("T-1", "-100", "EUR")], [rate(usd="1.16")], [])
    prepared = prepare_rows(raw, {"amount_override": {"T-1": "-500.00"}})
    assert prepared[0]["amt"] == Decimal("-500.00")


def test_output_sorted_by_txn_id():
    rows, _ = to_usd([row("T-2", "-1", "USD"), row("T-1", "-2", "USD")], [], [])
    assert [r["txn_id"] for r in rows] == ["T-1", "T-2"]


def test_applied_rate_recorded_on_row():
    rows, _ = to_usd([row("T-1", "-100", "EUR")], [rate(usd="1.16", quote="таблица курсов")], [])
    assert rows[0]["fx_applied"] == "1.16"
    assert rows[0]["fx_source_quote"] == "таблица курсов"
