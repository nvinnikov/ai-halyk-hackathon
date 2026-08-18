from dsl import parse, unparse
from rewrites import apply_final, narrow_opex

_EBITDA_BROAD = "sub(agg(REVENUE, in), agg(OPEX_TOTAL, out))"


def test_narrows_when_quote_says_nothing_about_articles():
    quote = "не допускать снижения EBITDA ниже $600,000.00 за период"
    ast, changed = narrow_opex(parse(_EBITDA_BROAD), quote)
    assert changed
    assert unparse(ast) == "sub(agg(REVENUE, in), agg(OTHER_OPEX, out))"


def test_keeps_rollup_when_quote_enumerates_articles():
    quote = (
        "EBITDA означает Выручку за вычетом Операционных расходов, включая "
        "расходы на оплату труда, арендные платежи и коммунальные расходы"
    )
    ast, changed = narrow_opex(parse(_EBITDA_BROAD), quote)
    assert not changed
    assert unparse(ast) == _EBITDA_BROAD


def test_keeps_rollup_when_quote_says_all_operating_expenses():
    quote = "за вычетом ВСЕХ операционных расходов за период"
    _ast, changed = narrow_opex(parse(_EBITDA_BROAD), quote)
    assert not changed


def test_does_not_touch_opex_outside_ebitda():
    metric = "ratio(agg(CONSULTING, out), agg(OPEX_TOTAL, out))"
    ast, changed = narrow_opex(parse(metric), "доля консультационных в операционных расходах")
    assert not changed
    assert unparse(ast) == metric


def test_apply_final_reports_alarm_and_rewrites_text():
    spec = {"metric_ast": parse(_EBITDA_BROAD), "metric_text": _EBITDA_BROAD}
    new, alarms = apply_final(spec, "не допускать снижения EBITDA ниже $600,000.00")
    assert new["metric_text"] == "sub(agg(REVENUE, in), agg(OTHER_OPEX, out))"
    assert [a["kind"] for a in alarms] == ["opex_rollup_narrowed"]
    assert spec["metric_text"] == _EBITDA_BROAD  # исходный cellspec не мутирован


def test_apply_final_is_noop_without_quote():
    spec = {"metric_ast": parse(_EBITDA_BROAD), "metric_text": _EBITDA_BROAD}
    new, alarms = apply_final(spec, "")
    assert new["metric_text"] == _EBITDA_BROAD
    assert alarms == []


def test_narrows_nested_sub_form():
    """Поправка оркестратора: несовпавший Sub обязан спускаться в детей, как
    любой другой узел, иначе вложенная форма sub(sub(...), ...) не
    переписывается вовсе."""
    metric = "sub(sub(agg(REVENUE, in), agg(OPEX_TOTAL, out)), agg(ONE_OFF, out))"
    ast, changed = narrow_opex(parse(metric), "не допускать снижения EBITDA")
    assert changed
    assert unparse(ast) == "sub(sub(agg(REVENUE, in), agg(OTHER_OPEX, out)), agg(ONE_OFF, out))"
