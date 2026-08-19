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


def test_inserted_adjective_between_quant_and_quarter_still_matches():
    """Раунд правок 1: буквальный список фраз рвался вставленным между
    квантором и «кварталом» прилагательным — F2 6.1 приватного набора пишет
    «в любом ОТДЕЛЬНОМ финансовом квартале», а старый список знал только
    «любом финансовом квартале» без разрыва."""
    quote = "не допускать превышения расходами в любом отдельном финансовом квартале величины $300,000.00"
    ast, changed = quarterly(parse("agg(MARKETING, out)"), quote, "max")
    assert changed
    assert unparse(ast).startswith("max(")


def test_english_inserted_word_between_quant_and_quarter_still_matches():
    quote = "EBITDA shall not fall below $600,000.00 in any single fiscal quarter"
    ast, changed = quarterly(parse(_EBITDA), quote, "min")
    assert changed
    assert unparse(ast).startswith("min(")


def test_quant_and_quarter_too_far_apart_does_not_match():
    """Отрицающий тест: квантор и «квартал» дальше допустимого разрыва слов
    (в соседнем предложении цитаты) — не квартализуем, иначе образец склеивал
    бы несвязанные части текста и квартализовал бы годовую метрику там, где
    пункт вообще не про кварталы."""
    quote = (
        "Компания вправе принять любое решение по своему усмотрению в течение "
        "финансового года. Отчётность предоставляется по результатам работы "
        "подразделения за квартал."
    )
    ast, changed = quarterly(parse(_EBITDA), quote, "min")
    assert not changed


def test_sentence_boundary_blocks_window_even_with_one_word_gap():
    """Раунд правок 2 (Important): окно разрыва не должно перескакивать точку.
    Квантор и «квартал» разделены ровно одним словом, но это слово —
    последнее в своём предложении, а «квартал» открывает следующее. Цитаты
    пунктов в этих договорах многосоставны (см. диагностику F2 6.1 в раунде
    0) — точка внутри одной цитаты не крайний случай, а наблюдаемая форма."""
    quote = "Любой платёж. Квартал начинается заново."
    ast, changed = quarterly(parse(_EBITDA), quote, "min")
    assert not changed


def test_ratio_nested_deeper_in_tree_is_left_alone():
    """Раунд правок 2 (Minor): отказ на отношениях смотрит на всё дерево, а
    не только на корень — извлечённая формула вправе положить ratio() глубже
    (например, внутри add())."""
    metric = "add(ratio(agg(CAPEX, out), agg(REVENUE, in)), agg(ONE_OFF, out))"
    quote = "не допускать снижения показателя за любой финансовый квартал"
    ast, changed = quarterly(parse(metric), quote, "min")
    assert not changed
    assert unparse(ast) == metric


# --- §5 финального ревью: сужение опекса применяется и к триггеру ------------


def _spec_with_trigger(trigger: str, metric: str = _EBITDA_BROAD) -> dict:
    return {
        "metric_ast": parse(metric),
        "metric_text": metric,
        "trigger_ast": parse(trigger),
        "direction": "min",
    }


def test_narrows_the_trigger_together_with_the_metric():
    """Выбор прочтения EBITDA — свойство договора, а не места формулы.

    EBITDA, посчитанная в метрике по статье, а в условии применимости по
    роллапу, отличалась бы на два порядка, и цена этого — статус: несработавший
    триггер даёт безусловный COMPLIANT."""
    spec = _spec_with_trigger(f"lt({_EBITDA_BROAD}, const(1000))")
    new, alarms = apply_final(spec, "не допускать снижения EBITDA ниже $600,000.00")
    assert unparse(new["metric_ast"]) == "sub(agg(REVENUE, in), agg(OTHER_OPEX, out))"
    assert unparse(new["trigger_ast"]) == "lt(sub(agg(REVENUE, in), agg(OTHER_OPEX, out)), const(1000))"
    assert [(a["kind"], a["target"]) for a in alarms] == [
        ("opex_rollup_narrowed", "metric"),
        ("opex_rollup_narrowed", "trigger"),
    ]
    assert unparse(spec["trigger_ast"]) == f"lt({_EBITDA_BROAD}, const(1000))"  # вход не мутирован


def test_leaves_a_trigger_without_the_rollup_alone():
    trigger = "gt(agg(FINANCING, in), const(500))"
    spec = _spec_with_trigger(trigger)
    new, alarms = apply_final(spec, "не допускать снижения EBITDA ниже $600,000.00")
    assert unparse(new["trigger_ast"]) == trigger
    assert [a["target"] for a in alarms] == ["metric"]


def test_quarterization_never_touches_the_trigger():
    """Квартальным бывает именно условие («если выручка любого квартала ниже
    X»), поэтому квартализация остаётся правкой одной метрики."""
    trigger = "lt(agg(REVENUE, in), const(500))"
    spec = _spec_with_trigger(trigger, metric="agg(REVENUE, in)")
    new, alarms = apply_final(spec, "в любом отдельном финансовом квартале", direction="min")
    assert [a["kind"] for a in alarms] == ["metric_quarterized"]
    assert unparse(new["trigger_ast"]) == trigger
