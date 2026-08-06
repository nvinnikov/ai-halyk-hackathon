"""OTHER — корзина потерянного: не входит в роллапы, растёт — алярм."""

from decimal import Decimal

import pytest

from taxonomy import LEAVES, ROLLUPS, coverage_report, expand, is_category


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
