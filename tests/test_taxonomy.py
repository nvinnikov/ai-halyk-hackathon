"""OTHER — корзина потерянного: не входит в роллапы, растёт — алярм."""

from decimal import Decimal

import pytest

from taxonomy import LEAVES, ROLLUPS, cell_other_alarm, coverage_report, expand, is_category


def test_leaves_and_rollups_disjoint():
    assert not LEAVES & set(ROLLUPS)


def test_expand():
    assert expand("PAYROLL") == frozenset({"PAYROLL"})
    assert "PAYROLL" in expand("OPEX_TOTAL")
    assert "OTHER" not in expand("OPEX_TOTAL")
    assert expand("ALL") == LEAVES
    with pytest.raises(KeyError):
        expand("NOPE")


def test_is_category():
    assert is_category("REVENUE") and is_category("OPEX_TOTAL")
    assert not is_category("nope")


def rows(*pairs):
    return [{"txn_id": f"T-{i}", "cat": c, "amt": Decimal(a)} for i, (c, a) in enumerate(pairs)]


def test_coverage_by_sum_not_by_count():
    r = rows(("PAYROLL", "-1"), ("OTHER", "-99"))
    rep = coverage_report(r)
    assert rep["other_share"] == pytest.approx(0.99)
    assert rep["alarm"] == "warn"


def test_critical_when_covenant_touches_lost_category():
    r = rows(("PAYROLL", "-1"), ("OTHER", "-99"))
    assert coverage_report(r, referenced={"OPEX_TOTAL"})["alarm"] == "critical"


def test_clean_ledger_no_alarm():
    r = rows(("PAYROLL", "-100"), ("REVENUE", "200"))
    assert coverage_report(r)["alarm"] == "none"


def test_rules_emit_only_leaves():
    """Категория, которую выдают правила, но которой нет в таксономии, выпала бы
    из всех роллапов молча — как случилось с OPEX при переименовании."""
    from categorize import RULES

    assert {name for name, _ in RULES} <= LEAVES


def test_reclass_targets_are_leaves():
    """То же для реклассификаций эталона: `to` мимо таксономии не считается нигде."""
    from expected_extraction import FACTS

    targets = {rc["to"] for f in FACTS.values() for rc in f.get("reclass", [])}
    assert targets <= LEAVES


def _row(txn: str, cat: str, amt: str) -> dict:
    return {"txn_id": txn, "cat": cat, "amt": Decimal(amt)}


def test_no_alarm_when_other_empty():
    """Нет неразнесённых строк — нет и алярма."""
    rows = [_row("T-1", "REVENUE", "100"), _row("T-2", "CAPEX", "-50")]
    assert cell_other_alarm(rows, {"REVENUE"}) is None


def test_no_alarm_when_metric_reads_all():
    """ALL включает OTHER: неразнесённые строки метрика и так считает."""
    rows = [_row("T-1", "REVENUE", "100"), _row("T-2", "OTHER", "-40")]
    assert cell_other_alarm(rows, {"ALL"}) is None


def test_alarm_when_blind_category_and_other_present():
    """Метрика читает REVENUE, часть суммы осела в OTHER — потеря молчаливая."""
    rows = [_row("T-1", "REVENUE", "100"), _row("T-2", "OTHER", "-25")]
    a = cell_other_alarm(rows, {"REVENUE"})
    assert a is not None
    assert a["blind"] == ["REVENUE"]
    assert a["other_sum"] == "25"
    assert a["inputs_sum"] == "100"
    assert a["severity"] == "0.250000"
    assert a["txn_ids"] == ["T-2"]


def test_rollup_expanded_to_leaves():
    """Роллап разворачивается: OPEX_TOTAL слеп к OTHER так же, как его листья."""
    rows = [_row("T-1", "PAYROLL", "-80"), _row("T-2", "RENT", "-20"), _row("T-3", "OTHER", "-10")]
    a = cell_other_alarm(rows, {"OPEX_TOTAL"})
    assert a is not None
    assert a["inputs_sum"] == "100"  # PAYROLL + RENT, оба листья OPEX_TOTAL


def test_severity_none_when_metric_inputs_empty():
    """Метрика читает категорию, где строк нет вовсе: severity не считается,
    но алярм есть — это максимальная тяжесть, а не её отсутствие."""
    rows = [_row("T-1", "OTHER", "-10")]
    a = cell_other_alarm(rows, {"CAPEX"})
    assert a is not None
    assert a["severity"] is None
    assert a["inputs_sum"] == "0"


def test_unknown_category_treated_as_blind():
    """Незнакомая категория считается слепой: fail-open не должен молчать."""
    rows = [_row("T-1", "OTHER", "-10"), _row("T-2", "REVENUE", "50")]
    a = cell_other_alarm(rows, {"NOT_A_CATEGORY"})
    assert a is not None
    assert a["blind"] == ["NOT_A_CATEGORY"]


def test_no_alarm_without_referenced():
    """Категории метрики неизвестны — судить не о чем."""
    assert cell_other_alarm([_row("T-1", "OTHER", "-10")], set()) is None


def test_deterministic_output():
    """Порядок blind и txn_ids не зависит от порядка входа."""
    rows = [_row("T-9", "OTHER", "-1"), _row("T-1", "OTHER", "-2"), _row("T-5", "REVENUE", "10")]
    a = cell_other_alarm(rows, {"REVENUE", "CAPEX"})
    b = cell_other_alarm(list(reversed(rows)), {"CAPEX", "REVENUE"})
    assert a == b
    assert a["blind"] == ["CAPEX", "REVENUE"]
    assert a["txn_ids"] == ["T-1", "T-9"]
