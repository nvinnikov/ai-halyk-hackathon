"""Проводка диагностик ячейки: они обязаны смотреть на ПОСЧИТАННУЮ метрику.

run_cell прогоняет спеку через rewrites.apply_final и считает уже переписанную
формулу, а исходный cellspec не мутируется. Диагностики живут снаружи расчёта и
получают тот же исходный cellspec, поэтому «читают ли они переписанное» — это
проводка, а не свойство самих диагностик, и до этих тестов её не проверял
никто: ревью финальной ветки нашло на артефактах приватного прогона пять ячеек,
где формула считала статью опекса, а sign_divergence докладывал про роллап.
"""

from decimal import Decimal

import noise
import solve
from dsl import parse

_EBITDA_BROAD = "sub(agg(REVENUE, in), agg(OPEX_TOTAL, out))"
_EBITDA_NARROW = "sub(agg(REVENUE, in), agg(OTHER_OPEX, out))"

# Цитата ничего не говорит о составе EBITDA — значит действует умолчание по
# статье, и narrow_opex сужает роллап.
_QUOTE_SILENT = "не допускать снижения EBITDA ниже 100 за период"
# Цитата прямо требует широкого прочтения — переписывания не будет.
_QUOTE_BROAD = "EBITDA за вычетом всех операционных расходов не ниже 100"


def _row(txn: str, cat: str, amt: str) -> dict:
    return {
        "txn_id": txn,
        "account_id": "ACC-0000",
        "counterparty": "Contoso",
        "description": "",
        "date": "2025-06-01",
        "cat": cat,
        "amt": Decimal(amt),
    }


def _rows() -> list[dict]:
    return [
        _row("TXN-1", "REVENUE", "1000"),
        _row("TXN-2", "OTHER_OPEX", "-500"),
        _row("TXN-3", "OTHER_OPEX", "50"),  # сторно внутри статьи
        _row("TXN-4", "PAYROLL", "-300"),  # входит только в роллап
        _row("TXN-5", "OTHER", "-20"),  # неразнесённая строка
    ]


def _cellspec(metric: str = _EBITDA_BROAD, trigger: str | None = None) -> dict:
    return {
        "metric_ast": parse(metric),
        "metric_text": metric,
        "direction": "min",
        "limit": Decimal("100"),
        "trigger_ast": parse(trigger) if trigger else None,
    }


def _run(quote: str, cellspec: dict | None = None) -> dict:
    """run_cell + диагностики ровно в том порядке и с теми же аргументами, что в main."""
    rows = _rows()
    cellspec = cellspec if cellspec is not None else _cellspec()
    _cell, trace = solve.run_cell("SC-D", "6.1", rows, {}, cellspec, [], quote=quote)
    solve._cell_diagnostics(trace, rows, cellspec, quote, "ACC-0000", noise.POLLUTION_LEVEL, "SC-D", "6.1")
    return trace


def test_diagnostics_speak_about_the_category_the_cell_actually_counted():
    trace = _run(_QUOTE_SILENT)
    assert trace["formula"] == _EBITDA_NARROW  # ячейку посчитала суженная формула
    # Сторно есть и в статье, и в роллапе (роллап включает статью), поэтому
    # непереписанный AST дал бы здесь OPEX_TOTAL — ровно ложное срабатывание
    # с пяти ячеек приватного прогона.
    assert list(trace["sign_divergence"]) == ["OTHER_OPEX"]
    # Счёт «загрязнён» по входному признаку, но суженная метрика роллапа не
    # читает — алярма быть не должно.
    assert not [a for a in trace.get("alarms", []) if a["kind"] == "polluted_rollup_read"]
    # Тяжесть неразнесённых меряется знаменателем из категорий метрики: у
    # роллапа туда попал бы ещё и ФОТ, то есть тяжесть занижалась бы.
    assert trace["other_unassigned"]["blind"] == ["OTHER_OPEX", "REVENUE"]


def test_the_same_diagnostics_report_the_rollup_when_the_quote_keeps_it():
    """Контроль: без переписывания все три говорят про роллап.

    Без этого теста первый доказывал бы только то, что диагностики молчат."""
    trace = _run(_QUOTE_BROAD)
    assert trace["formula"] == _EBITDA_BROAD
    assert list(trace["sign_divergence"]) == ["OPEX_TOTAL"]
    got = [a for a in trace["alarms"] if a["kind"] == "polluted_rollup_read"]
    assert got and got[0]["categories"] == ["OPEX_TOTAL"]
    assert trace["other_unassigned"]["blind"] == ["OPEX_TOTAL", "REVENUE"]


def test_diagnostics_see_the_rewritten_trigger_too():
    trace = _run(_QUOTE_SILENT, _cellspec(trigger=f"gt({_EBITDA_BROAD}, const(0))"))
    # Категории триггера учитываются наравне с категориями метрики, и триггер
    # сужается тем же apply_final: роллапа в списке слепых быть не должно.
    assert trace["other_unassigned"]["blind"] == ["OTHER_OPEX", "REVENUE"]


def test_a_broken_rewrite_leaves_the_diagnostics_on_the_original_metric(monkeypatch):
    """Сбой переписывания не стоит ни ячейки, ни диагностик: врущая диагностика
    дешевле отсутствующей, а ошибка называется в трейсе."""
    rows = _rows()
    cellspec = _cellspec()

    def boom(*_a, **_k):
        raise RuntimeError("обход AST сломался")

    monkeypatch.setattr(solve.rewrites, "apply_final", boom)
    trace: dict = {}
    solve._cell_diagnostics(
        trace, rows, cellspec, _QUOTE_SILENT, "ACC-0000", noise.POLLUTION_LEVEL, "SC-D", "6.1"
    )
    assert "RuntimeError" in trace["diagnostics_rewrite_error"]
    assert list(trace["sign_divergence"]) == ["OPEX_TOTAL"]


# --- §2: сбой переписывания не имеет права выбросить ячейку мимо лестницы ----


def test_rewrite_failure_keeps_the_cell_computed(monkeypatch):
    """apply_final стоит до общего try run_cell, и без собственного перехвата
    его исключение улетало бы во внешний except main, где ячейка — ещё скелет:
    прочитанные направление и порог выброшены, actual без порога, приор без
    семьи. Инвариант fail-open требует обратного."""
    rows = _rows()

    def boom(*_a, **_k):
        raise RuntimeError("unparse нового корня сломался")

    monkeypatch.setattr(solve.rewrites, "apply_final", boom)
    cell, trace = solve.run_cell("SC-D", "6.1", rows, {}, _cellspec(), [], quote=_QUOTE_SILENT)
    assert "RuntimeError" in trace["rewrite_error"]
    assert trace["tier"] == 0 and trace["path"] == "dsl"
    # Считалась исходная, несуженная метрика: 1000 − (500 статья + 300 ФОТ).
    assert cell["status"] == "COMPLIANT" and cell["actual"] == 200.0
    assert trace["spec"]["limit"] == "100"


# --- §3: обе стороны тени проходят одни и те же финальные переписывания ------


def test_shadow_compares_the_extracted_formula_after_the_same_rewrites():
    """Тень отвечает на вопрос «изменила ли ответ ПОДМЕНА ЗАГОЛОВКА», а не
    «подмена и переписывания вместе». Шаблон здесь уже узкий, извлечённая
    формула — широкая; после общего сужения обе считают одно и то же, и
    поимённый список ячеек для ручного разбора не должен получить лишнее имя.
    """
    cellspec = _cellspec(_EBITDA_NARROW)
    cellspec["shadow_metric_text"] = _EBITDA_BROAD
    _cell, trace = solve.run_cell("SC-D", "6.1", _rows(), {}, cellspec, [], quote=_QUOTE_SILENT)
    assert trace["shadow"]["metric"] == _EBITDA_BROAD  # что извлекла модель
    assert trace["shadow"]["metric_computed"] == _EBITDA_NARROW  # что посчитала тень
    assert trace["shadow"]["changed_answer"] is False
    assert not [a for a in trace.get("alarms", []) if a["kind"] == "heading_divergence_changed_answer"]


def test_shadow_still_reports_a_divergence_created_by_the_substitution():
    """Контроль: сужение обеих сторон не глушит настоящее расхождение."""
    cellspec = _cellspec("agg(CAPEX, out)")
    cellspec["direction"] = "max"
    cellspec["shadow_metric_text"] = _EBITDA_BROAD
    _cell, trace = solve.run_cell("SC-D", "6.1", _rows(), {}, cellspec, [], quote=_QUOTE_SILENT)
    assert trace["shadow"]["metric_computed"] == _EBITDA_NARROW
    assert trace["shadow"]["changed_answer"] is True
