"""Пустая и неверная ячейка стоят одинаково — ноль; ответ обязана получить каждая."""

from decimal import Decimal

from dsl import parse
from fallbacks import fallback_cell, family_of, heuristic_template, load_prior, prior_status


def test_family_of():
    assert family_of(parse("ratio(agg(CAPEX, out), agg(REVENUE, in))"), Decimal("9")) == "ratio"
    assert family_of(parse("ratio(agg(CAPEX, out), agg(REVENUE, in))"), Decimal("0.04")) == "share"
    assert family_of(parse("agg(CAPEX, out)"), Decimal("2000000")) == "absolute"
    assert family_of(None, None) is None


def test_prior_status_conditional_and_global():
    prior = load_prior()
    status, conditional = prior_status(prior, "max", "absolute")
    assert status in ("BREACH", "COMPLIANT") and conditional is True
    status, conditional = prior_status(prior, None, None)
    assert status in ("BREACH", "COMPLIANT") and conditional is False


def test_heuristic_template_keywords():
    assert heuristic_template("платежи связанным сторонам не превышают") == "related_abs"
    assert heuristic_template("capital expenditures shall not exceed") == "capex"
    assert heuristic_template("минимальная выручка за год") == "revenue"
    assert heuristic_template("что-то невнятное") is None


def test_fallback_cell_actual_ladder():
    # порог известен → actual = порог
    cell, alarms = fallback_cell("max", "absolute", Decimal("500000"), [])
    assert cell["actual"] == 500000.0 and cell["evidence_txn_id"] is None
    # порога нет → медиана посчитанных с тем же направлением
    cell, _ = fallback_cell("max", None, None, [("max", 10.0), ("max", 30.0), ("min", 999.0)])
    assert cell["actual"] == 20.0
    # нет ничего → 1.0 и алярм подбрасывания монеты
    cell, alarms = fallback_cell(None, None, None, [])
    assert cell["actual"] == 1.0
    assert "fallback_coin_flip" in alarms


def test_prior_loads_from_any_cwd(monkeypatch, tmp_path):
    """Прогон могут запустить не из корня — приор обязан найтись через ROOT,
    иначе skeleton() падает до первой ячейки и submission не создаётся."""
    import fallbacks

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fallbacks, "_prior_cache", None)
    assert "global" in fallbacks.load_prior()


def test_fallback_cell_always_complete():
    cell, _ = fallback_cell(None, None, None, [])
    assert cell["status"] in ("BREACH", "COMPLIANT")
    assert isinstance(cell["actual"], float)
