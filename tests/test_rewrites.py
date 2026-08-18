from dsl import parse, unparse
from rewrites import apply_final, narrow_opex, quarterly

_EBITDA_BROAD = "sub(agg(REVENUE, in), agg(OPEX_TOTAL, out))"


def test_narrows_when_quote_says_nothing_about_articles():
    quote = "не допускать снижения EBITDA ниже $600,000.00 за период"
    ast, changed = narrow_opex(parse(_EBITDA_BROAD), quote)
    assert changed
    assert unparse(ast) == "sub(agg(REVENUE, in), agg(OTHER_OPEX, out))"


def test_narrows_when_articles_describe_covenant_denominator():
    """Раунд правок 1 (рулинг team-lead): подсчёт маркеров-статей по ВСЕЙ
    цитате неспасаем — H5 6.2 приватного набора перечисляет аренду,
    коммунальные и страхование как ЗНАМЕНАТЕЛЬ ковенанта (сумма фиксированных
    платежей), а не как состав вычитаемого в EBITDA, и старая эвристика
    ошибочно сохраняла роллап OPEX_TOTAL. Правильный ответ — сузить: цитата
    ничего не говорит о СОСТАВЕ EBITDA, значит действует умолчание по статье."""
    quote = (
        "Отношение EBITDA Заёмщика к сумме арендных платежей, коммунальных "
        "расходов и страховых премий за тот же период должно составлять не "
        "менее 2.28x."
    )
    ast, changed = narrow_opex(parse(_EBITDA_BROAD), quote)
    assert changed
    assert unparse(ast) == "sub(agg(REVENUE, in), agg(OTHER_OPEX, out))"


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


def test_narrows_composite_subtrahend():
    """Раунд правок 1 (рулинг team-lead, дефект №2): J1 6.3 приватного набора
    вычитает названные статьи СУММОЙ — sub(REVENUE, add(OPEX_TOTAL, PAYROLL,
    RENT)) — и роллап внутри add так же ошибочен, как голый agg. Границу не
    расширяем дальше: узнаём только OPEX_TOTAL среди ПРЯМЫХ аргументов add
    под EBITDA-подвыражением, остальные статьи (PAYROLL, RENT) не трогаем."""
    metric = "sub(agg(REVENUE, in), add(agg(OPEX_TOTAL, out), agg(PAYROLL, out), agg(RENT, out)))"
    ast, changed = narrow_opex(parse(metric), "не допускать снижения показателя")
    assert changed
    assert unparse(ast) == (
        "sub(agg(REVENUE, in), add(agg(OTHER_OPEX, out), agg(PAYROLL, out), agg(RENT, out)))"
    )


_EBITDA = "sub(agg(REVENUE, in), agg(OTHER_OPEX, out))"


def test_min_covenant_becomes_min_over_quarters():
    quote = "не допускать снижения EBITDA за любой финансовый квартал ниже $600,000.00"
    ast, changed = quarterly(parse(_EBITDA), quote, "min")
    assert changed
    text = unparse(ast)
    assert text.startswith("min(")
    assert text.count("quarter(1)") == 2
    assert text.count("quarter(4)") == 2


def test_max_covenant_becomes_max_over_quarters():
    quote = "совокупные маркетинговые расходы за любой финансовый квартал не превысят $300,000.00"
    ast, changed = quarterly(parse("agg(MARKETING, out)"), quote, "max")
    assert changed
    assert unparse(ast).startswith("max(")


def test_english_marker_is_recognised():
    ast, changed = quarterly(parse("agg(REVENUE, in)"), "Revenue in any fiscal quarter", "min")
    assert changed
    assert unparse(ast).startswith("min(")


def test_ratio_metric_is_left_alone():
    metric = "ratio(agg(CAPEX, out), agg(REVENUE, in))"
    ast, changed = quarterly(parse(metric), "если выручка за любой финансовый квартал ниже", "max")
    assert not changed
    assert unparse(ast) == metric


def test_metric_already_quarterly_is_left_alone():
    metric = "agg(REVENUE, in, quarter(4))"
    ast, changed = quarterly(parse(metric), "выручка за любой финансовый квартал", "min")
    assert not changed
    assert unparse(ast) == metric


def test_no_marker_no_rewrite():
    ast, changed = quarterly(parse(_EBITDA), "EBITDA за период с 2025-01-01 по 2025-12-31", "min")
    assert not changed


def test_period_filter_is_replaced_by_quarter():
    metric = "agg(REVENUE, in, period(2025-01-01, 2025-12-31))"
    ast, _ = quarterly(parse(metric), "выручка за любой финансовый квартал", "min")
    text = unparse(ast)
    assert "period(" not in text
    assert "quarter(2)" in text
