from dsl import parse, unparse
from rewrites import (
    add_ebitda_addback,
    apply_final,
    cap_related_party_basket,
    flip_debt_incurrence_sign,
    narrow_opex,
    quarterly,
    widen_related_party,
)

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


# --- Task 1: платежи связанным сторонам читают все категории ----------------


def test_widens_narrow_category_under_related_party_filter():
    """Ковенант об оттоке к связанным сторонам не завязан на статью учёта:
    FINANCING/OTHER — промах модели, реальный признак — фильтр
    counterparty_in(related_parties)."""
    metric = "agg(FINANCING, out, counterparty_in(related_parties))"
    ast, changed = widen_related_party(parse(metric), "платежи связанным сторонам")
    assert changed
    assert unparse(ast) == "agg(ALL, out, counterparty_in(related_parties))"


def test_widens_other_category_under_related_party_filter():
    metric = "agg(OTHER, out, period(2025-01-01, 2025-12-31), counterparty_in(related_parties))"
    ast, changed = widen_related_party(parse(metric), "выплаты аффилированным лицам")
    assert changed
    assert unparse(ast) == "agg(ALL, out, period(2025-01-01, 2025-12-31), counterparty_in(related_parties))"


def test_leaves_agg_without_related_party_filter_alone():
    metric = "agg(FINANCING, out)"
    ast, changed = widen_related_party(parse(metric), "погашение задолженности")
    assert not changed
    assert unparse(ast) == metric


def test_leaves_agg_already_rolled_up_alone():
    metric = "agg(ALL, out, counterparty_in(related_parties))"
    ast, changed = widen_related_party(parse(metric), "платежи связанным сторонам")
    assert not changed
    assert unparse(ast) == metric


def test_ratio_traversal_leaves_already_widened_numerator_and_unfiltered_denominator_alone():
    """Проверяет только обход дерева через Ratio: числитель уже ALL (не
    подходит под условие «лист»), знаменатель без фильтра связанных сторон
    (не подходит под условие «есть фильтр») — ни один узел не проходит вход
    в переписывание независимо от защиты знаменателя. Защиту знаменателя как
    таковую эта пара не проверяет — см.
    test_leaves_leaf_category_with_related_party_filter_in_denominator_alone."""
    metric = "ratio(agg(ALL, out, counterparty_in(related_parties)), agg(OTHER_OPEX, out))"
    ast, changed = widen_related_party(parse(metric), "доля платежей связанным сторонам")
    assert not changed
    assert unparse(ast) == metric


def test_widens_agg_inside_ratio_numerator():
    """Узкая статья под фильтром связанных сторон расширяется и внутри
    Ratio.num, а не только на корне дерева — обход спускается в num, а не
    только в den."""
    metric = "ratio(agg(FINANCING, out, counterparty_in(related_parties)), agg(REVENUE, in))"
    ast, changed = widen_related_party(parse(metric), "платежи связанным сторонам")
    assert changed
    assert unparse(ast) == "ratio(agg(ALL, out, counterparty_in(related_parties)), agg(REVENUE, in))"


def test_leaves_leaf_category_with_related_party_filter_in_denominator_alone():
    """Прямая проверка защиты знаменателя (раунд правок 1, рулинг ревьюера):
    предыдущие «знаменательные» тесты клали в знаменатель агрегат, который
    ни при каких условиях не подходил под вход переписывания (либо не лист,
    либо без фильтра связанных сторон), и потому не отличали защищённую
    реализацию от снятой — оба варианта проходили все тесты.

    Здесь в знаменателе стоит ЕДИНСТВЕННЫЙ вход, который вообще включает
    переписывание: лист таксономии с фильтром counterparty_in(related_parties).
    Форма осмысленна — «доля общих платежей связанным сторонам в тех же
    платежах, проведённых по конкретной статье» — числитель обязан
    расшириться, знаменатель (та же статья) обязан остаться, иначе метрика
    считала бы саму себя в знаменателе."""
    metric = (
        "ratio(agg(FINANCING, out, counterparty_in(related_parties)), "
        "agg(CONSULTING, out, counterparty_in(related_parties)))"
    )
    ast, changed = widen_related_party(parse(metric), "доля платежей связанным сторонам")
    assert changed
    assert unparse(ast) == (
        "ratio(agg(ALL, out, counterparty_in(related_parties)), "
        "agg(CONSULTING, out, counterparty_in(related_parties)))"
    )


def test_apply_final_widens_related_party_and_reports_alarm():
    metric = "agg(FINANCING, out, counterparty_in(related_parties))"
    spec = {"metric_ast": parse(metric), "metric_text": metric}
    new, alarms = apply_final(spec, "платежи связанным сторонам")
    assert new["metric_text"] == "agg(ALL, out, counterparty_in(related_parties))"
    assert "related_party_widened" in [a["kind"] for a in alarms]
    assert spec["metric_text"] == metric  # исходный cellspec не мутирован


# --- Task 2: привлечённая задолженность — это приток -----------------------


def test_flips_financing_out_to_in_when_quote_signals_incurrence():
    """«Совокупная основная сумма Финансовой задолженности, привлечённой за
    период» — это приток, а модель извлекла отток."""
    metric = "agg(FINANCING, out)"
    quote = "совокупная основная сумма Финансовой задолженности, привлечённой за период"
    ast, changed = flip_debt_incurrence_sign(parse(metric), quote)
    assert changed
    assert unparse(ast) == "agg(FINANCING, in)"


def test_flips_financing_out_to_in_for_english_incurrence_quote():
    metric = "agg(FINANCING, out)"
    quote = "the aggregate principal amount of Financial Indebtedness incurred during the period"
    ast, changed = flip_debt_incurrence_sign(parse(metric), quote)
    assert changed
    assert unparse(ast) == "agg(FINANCING, in)"


def test_does_not_flip_when_quote_signals_repayment():
    metric = "agg(FINANCING, out)"
    ast, changed = flip_debt_incurrence_sign(parse(metric), "погашение основного долга по кредитам")
    assert not changed
    assert unparse(ast) == metric


def test_does_not_flip_when_quote_mentions_both_incurrence_and_repayment():
    """DSCR читает обе стороны в одной формуле — угадать здесь нельзя,
    переписывание обязано промолчать целиком."""
    metric = "agg(FINANCING, out)"
    quote = (
        "коэффициент покрытия обслуживания долга учитывает как привлечение "
        "нового финансирования, так и погашение основного долга за период"
    )
    ast, changed = flip_debt_incurrence_sign(parse(metric), quote)
    assert not changed
    assert unparse(ast) == metric


def test_does_not_flip_other_category_under_incurrence_quote():
    metric = "agg(OTHER, out)"
    ast, changed = flip_debt_incurrence_sign(parse(metric), "задолженность, привлечённая за период")
    assert not changed
    assert unparse(ast) == metric


def test_does_not_flip_financing_already_in():
    metric = "agg(FINANCING, in)"
    ast, changed = flip_debt_incurrence_sign(parse(metric), "задолженность, привлечённая за период")
    assert not changed
    assert unparse(ast) == metric


def test_flips_financing_nested_inside_add():
    metric = "add(agg(FINANCING, out), agg(OTHER, out))"
    ast, changed = flip_debt_incurrence_sign(parse(metric), "задолженность, привлечённая за период")
    assert changed
    assert unparse(ast) == "add(agg(FINANCING, in), agg(OTHER, out))"


def test_apply_final_flips_financing_sign_and_reports_alarm():
    metric = "agg(FINANCING, out)"
    spec = {"metric_ast": parse(metric), "metric_text": metric}
    new, alarms = apply_final(spec, "задолженность, привлечённая за период")
    assert new["metric_text"] == "agg(FINANCING, in)"
    assert "financing_sign_flipped" in [a["kind"] for a in alarms]
    assert spec["metric_text"] == metric  # исходный cellspec не мутирован


# --- Task 2, раунд правок 1: отрицание и рискованные основы -----------------


def test_flips_despite_negated_repayment_word():
    """Находка 1: «непогашенная» — стандартный банковский термин про остаток,
    который ЕЩЁ предстоит погасить, а не про факт погашения. Substring-поиск
    без учёта отрицания видел бы в ней подстроку «погашен» и ошибочно
    отказывался чинить знак — направление безопасное (недочинка), но
    формулировка типовая."""
    metric = "agg(FINANCING, out)"
    quote = "совокупная непогашенная основная сумма задолженности, привлечённой за период"
    ast, changed = flip_debt_incurrence_sign(parse(metric), quote)
    assert changed
    assert unparse(ast) == "agg(FINANCING, in)"


def test_negated_repayment_word_alone_is_not_a_repayment_signal():
    """«Непогашенный остаток» сам по себе не сигнал ни привлечения, ни
    погашения — без слова о привлечении переписывание не должно сработать,
    но и не должно ошибочно решить, что это цитата про погашение."""
    metric = "agg(FINANCING, out)"
    ast, changed = flip_debt_incurrence_sign(parse(metric), "непогашенный остаток по договору")
    assert not changed
    assert unparse(ast) == metric


def test_flips_risky_stem_when_debt_word_is_nearby():
    """Находка 2: «наращивание»/«нарастание» сами по себе не про долг, но
    рядом со словом задолженности/долга — это и есть искомый сигнал."""
    metric = "agg(FINANCING, out)"
    quote = "наращивание совокупной Финансовой задолженности за период"
    ast, changed = flip_debt_incurrence_sign(parse(metric), quote)
    assert changed
    assert unparse(ast) == "agg(FINANCING, in)"


def test_does_not_flip_risky_stem_growth_of_unrelated_quantity():
    """«Наращивание» резервов — рост произвольной величины, не задолженности:
    без слова о долге рядом переписывание обязано промолчать, а не перевернуть
    знак активно неверно."""
    metric = "agg(FINANCING, out)"
    quote = "наращивание резервов на возможные потери в течение периода"
    ast, changed = flip_debt_incurrence_sign(parse(metric), quote)
    assert not changed
    assert unparse(ast) == metric


def test_does_not_flip_risky_stem_audit_sample_context():
    """«Выборка» в лексике аудиторского заключения — sample, а не выборка
    транша: без слова о долге рядом переписывание обязано промолчать."""
    metric = "agg(FINANCING, out)"
    quote = "аудиторская выборка составила 25% от общего числа операций за период"
    ast, changed = flip_debt_incurrence_sign(parse(metric), quote)
    assert not changed
    assert unparse(ast) == metric


def test_does_not_flip_net_sign():
    """Minor: код сравнивает со знаком out точно — net (уже свёрнутая нетто-
    величина) переписывать нельзя, и будущий рефакторинг на «не приток» не
    должен пройти мимо CI незамеченным."""
    metric = "agg(FINANCING, net)"
    ast, changed = flip_debt_incurrence_sign(parse(metric), "задолженность, привлечённая за период")
    assert not changed
    assert unparse(ast) == metric


# --- Task 3: разовые корректировки EBITDA (addback) -------------------------


def test_adds_addback_to_ebitda_subexpression_when_needed():
    """S3 6.1: договор требует учесть разовую корректировку EBITDA, и
    признак пришёл готовым булем (needs_addback=True из
    facts_extract._quote_requires_addback) — cам текст цитаты сюда не
    передаётся, только вердикт."""
    metric = "sub(agg(REVENUE, in), agg(OTHER_OPEX, out))"
    ast, changed = add_ebitda_addback(parse(metric), True)
    assert changed
    assert (
        unparse(ast)
        == "add(sub(agg(REVENUE, in), agg(OTHER_OPEX, out)), doc(ebitda_addbacks_material_total))"
    )


def test_does_not_add_addback_when_not_needed():
    metric = "sub(agg(REVENUE, in), agg(OTHER_OPEX, out))"
    ast, changed = add_ebitda_addback(parse(metric), False)
    assert not changed
    assert unparse(ast) == metric


def test_does_not_add_addback_when_doc_key_already_present():
    """Часть заёмщиков (X2 6.2/6.4) вписывает addback в формулу сама — модель
    прочла его прямо в тексте пункта. Повторное добавление удвоило бы
    корректировку, поэтому переписывание обязано молчать целиком."""
    metric = "add(sub(agg(REVENUE, in), agg(OTHER_OPEX, out)), doc(ebitda_addbacks_material_total))"
    ast, changed = add_ebitda_addback(parse(metric), True)
    assert not changed
    assert unparse(ast) == metric


def test_adds_addback_inside_ratio_denominator():
    metric = "ratio(agg(FINANCING, in), sub(agg(REVENUE, in), agg(OTHER_OPEX, out)))"
    ast, changed = add_ebitda_addback(parse(metric), True)
    assert changed
    assert unparse(ast) == (
        "ratio(agg(FINANCING, in), "
        "add(sub(agg(REVENUE, in), agg(OTHER_OPEX, out)), doc(ebitda_addbacks_material_total)))"
    )


def test_adds_addback_to_composite_opex_subtrahend():
    """Форма, где вычитаемое составное (несколько названных статей суммой) —
    та же граница, что у narrow_opex.test_narrows_composite_subtrahend."""
    metric = "sub(agg(REVENUE, in), add(agg(OTHER_OPEX, out), agg(PAYROLL, out)))"
    ast, changed = add_ebitda_addback(parse(metric), True)
    assert changed
    assert unparse(ast) == (
        "add(sub(agg(REVENUE, in), add(agg(OTHER_OPEX, out), agg(PAYROLL, out))), "
        "doc(ebitda_addbacks_material_total))"
    )


def test_does_not_touch_non_ebitda_sub():
    """Вычитаемое не про EBITDA (B3-паттерн, капнутая корзина): a — не
    REVENUE, переписывание не имеет права его трогать."""
    metric = "sub(agg(ALL, out, counterparty_in(related_parties)), doc(related_party_consulting_basket))"
    ast, changed = add_ebitda_addback(parse(metric), True)
    assert not changed
    assert unparse(ast) == metric


def test_apply_final_applies_addback_and_reports_alarm():
    metric = "ratio(agg(FINANCING, in), sub(agg(REVENUE, in), agg(OTHER_OPEX, out)))"
    spec = {"metric_ast": parse(metric), "metric_text": metric, "direction": "max"}
    new, alarms = apply_final(spec, "квоту не превышать", "max", True)
    assert new["metric_text"] == (
        "ratio(agg(FINANCING, in), "
        "add(sub(agg(REVENUE, in), agg(OTHER_OPEX, out)), doc(ebitda_addbacks_material_total)))"
    )
    assert "ebitda_addback_applied" in [a["kind"] for a in alarms]
    assert spec["metric_text"] == metric  # исходный cellspec не мутирован


def test_apply_final_does_not_add_addback_by_default():
    """Обратная совместимость: старые вызовы apply_final без четвёртого
    параметра ничего не переписывают — новый параметр обязан иметь дефолт."""
    metric = "sub(agg(REVENUE, in), agg(OTHER_OPEX, out))"
    spec = {"metric_ast": parse(metric), "metric_text": metric}
    new, alarms = apply_final(spec, "любая непустая цитата")
    assert new["metric_text"] == metric
    assert alarms == []


def test_apply_final_applies_addback_to_trigger_too():
    """Определение EBITDA — свойство договора, а не места формулы: тот же
    принцип, что у narrow_opex применительно к триггеру (apply_final,
    докстринг)."""
    metric = "agg(CAPEX, out)"
    trigger = "gt(sub(agg(REVENUE, in), agg(OTHER_OPEX, out)), const(1000))"
    spec = {
        "metric_ast": parse(metric),
        "metric_text": metric,
        "trigger_ast": parse(trigger),
    }
    new, alarms = apply_final(spec, "любая непустая цитата", None, True)
    assert unparse(new["trigger_ast"]) == (
        "gt(add(sub(agg(REVENUE, in), agg(OTHER_OPEX, out)), doc(ebitda_addbacks_material_total)), "
        "const(1000))"
    )
    assert "ebitda_addback_applied" in [a["kind"] for a in alarms]


# --- Task 4: капнутая разрешённая корзина связанных сторон -------------------

_BASKET_QUOTE_RU = (
    "после исключения разрешённой корзины в размере до $300,000.00 таких "
    "платежей, совершённых в обычном порядке и надлежащим образом "
    "квалифицированных как Консультационные услуги"
)

_BASKET_METRIC = (
    "sub(agg(ALL, out, period(2025-01-01, 2025-12-31), counterparty_in(related_parties)), "
    "doc(related_party_consulting_basket))"
)


def test_caps_basket_with_min_of_category_and_const():
    """B3 6.1: корзина — минимум из фактической суммы платежей, квалифицированных
    как названная статья, и потолка из цитаты, а не потолок целиком."""
    doc_facts = {"related_party_consulting_basket": "300000"}
    doc_fact_quotes = {"related_party_consulting_basket": _BASKET_QUOTE_RU}
    ast, changed = cap_related_party_basket(parse(_BASKET_METRIC), doc_facts, doc_fact_quotes)
    assert changed
    assert unparse(ast) == (
        "sub(agg(ALL, out, period(2025-01-01, 2025-12-31), counterparty_in(related_parties)), "
        "min(agg(CONSULTING, out, period(2025-01-01, 2025-12-31), counterparty_in(related_parties)), "
        "const(300000)))"
    )


def test_caps_basket_english_quote():
    metric = "sub(agg(ALL, out, counterparty_in(related_parties)), doc(rp_basket))"
    doc_facts = {"rp_basket": "500000"}
    doc_fact_quotes = {
        "rp_basket": (
            "excluding a permitted basket of up to $500,000.00 of such payments made in "
            "the ordinary course and properly qualified as Consulting Services"
        )
    }
    ast, changed = cap_related_party_basket(parse(metric), doc_facts, doc_fact_quotes)
    assert changed
    assert unparse(ast) == (
        "sub(agg(ALL, out, counterparty_in(related_parties)), "
        "min(agg(CONSULTING, out, counterparty_in(related_parties)), const(500000)))"
    )


def test_does_not_cap_when_doc_key_missing_from_facts():
    ast, changed = cap_related_party_basket(parse(_BASKET_METRIC), {}, {})
    assert not changed
    assert unparse(ast) == _BASKET_METRIC


def test_does_not_cap_when_value_not_echoed_in_quote():
    """Значение doc-ключа не совпадает с потолком, найденным в цитате: ключ
    несёт что-то другое, подменять его нельзя."""
    doc_facts = {"related_party_consulting_basket": "250000"}
    doc_fact_quotes = {"related_party_consulting_basket": _BASKET_QUOTE_RU}
    ast, changed = cap_related_party_basket(parse(_BASKET_METRIC), doc_facts, doc_fact_quotes)
    assert not changed
    assert unparse(ast) == _BASKET_METRIC


def test_does_not_cap_without_basket_wording():
    quote = "величина, которая учитывается при расчёте показателя за период, до $300,000.00"
    doc_facts = {"related_party_consulting_basket": "300000"}
    doc_fact_quotes = {"related_party_consulting_basket": quote}
    ast, changed = cap_related_party_basket(parse(_BASKET_METRIC), doc_facts, doc_fact_quotes)
    assert not changed
    assert unparse(ast) == _BASKET_METRIC


def test_does_not_cap_without_up_to_wording():
    quote = (
        "после исключения разрешённой корзины в размере $300,000.00 таких "
        "платежей, квалифицированных как Консультационные услуги"
    )
    doc_facts = {"related_party_consulting_basket": "300000"}
    doc_fact_quotes = {"related_party_consulting_basket": quote}
    ast, changed = cap_related_party_basket(parse(_BASKET_METRIC), doc_facts, doc_fact_quotes)
    assert not changed
    assert unparse(ast) == _BASKET_METRIC


def test_does_not_cap_when_statute_is_ambiguous():
    """Две подходящие категории — отказ: молчаливый выбор одной дороже отказа."""
    quote = (
        "после исключения разрешённой корзины в размере до $300,000.00 таких "
        "платежей, квалифицированных как Консультационные услуги или "
        "Маркетинговые расходы"
    )
    doc_facts = {"related_party_consulting_basket": "300000"}
    doc_fact_quotes = {"related_party_consulting_basket": quote}
    ast, changed = cap_related_party_basket(parse(_BASKET_METRIC), doc_facts, doc_fact_quotes)
    assert not changed
    assert unparse(ast) == _BASKET_METRIC


def test_does_not_cap_when_statute_is_unrecognized():
    quote = (
        "после исключения разрешённой корзины в размере до $300,000.00 "
        "таких платежей, квалифицированных как Специальные расходы"
    )
    doc_facts = {"related_party_consulting_basket": "300000"}
    doc_fact_quotes = {"related_party_consulting_basket": quote}
    ast, changed = cap_related_party_basket(parse(_BASKET_METRIC), doc_facts, doc_fact_quotes)
    assert not changed
    assert unparse(ast) == _BASKET_METRIC


def test_does_not_cap_without_related_party_filter_on_numerator():
    metric = "sub(agg(ALL, out, period(2025-01-01, 2025-12-31)), doc(related_party_consulting_basket))"
    doc_facts = {"related_party_consulting_basket": "300000"}
    doc_fact_quotes = {"related_party_consulting_basket": _BASKET_QUOTE_RU}
    ast, changed = cap_related_party_basket(parse(metric), doc_facts, doc_fact_quotes)
    assert not changed
    assert unparse(ast) == metric


def test_does_not_touch_sub_without_doc_subtrahend():
    metric = "sub(agg(REVENUE, in), agg(OTHER_OPEX, out))"
    doc_facts = {"related_party_consulting_basket": "300000"}
    doc_fact_quotes = {"related_party_consulting_basket": _BASKET_QUOTE_RU}
    ast, changed = cap_related_party_basket(parse(metric), doc_facts, doc_fact_quotes)
    assert not changed
    assert unparse(ast) == metric


def test_caps_basket_nested_inside_add():
    metric = f"add({_BASKET_METRIC}, agg(ONE_OFF, out))"
    doc_facts = {"related_party_consulting_basket": "300000"}
    doc_fact_quotes = {"related_party_consulting_basket": _BASKET_QUOTE_RU}
    ast, changed = cap_related_party_basket(parse(metric), doc_facts, doc_fact_quotes)
    assert changed
    assert unparse(ast) == (
        "add(sub(agg(ALL, out, period(2025-01-01, 2025-12-31), counterparty_in(related_parties)), "
        "min(agg(CONSULTING, out, period(2025-01-01, 2025-12-31), counterparty_in(related_parties)), "
        "const(300000))), agg(ONE_OFF, out))"
    )


def test_apply_final_caps_basket_and_reports_alarm():
    spec = {
        "metric_ast": parse(_BASKET_METRIC),
        "metric_text": _BASKET_METRIC,
        "direction": "max",
    }
    doc_facts = {"related_party_consulting_basket": "300000"}
    doc_fact_quotes = {"related_party_consulting_basket": _BASKET_QUOTE_RU}
    new, alarms = apply_final(
        spec, "квоту не превышать", "max", False, doc_facts=doc_facts, doc_fact_quotes=doc_fact_quotes
    )
    assert new["metric_text"] == (
        "sub(agg(ALL, out, period(2025-01-01, 2025-12-31), counterparty_in(related_parties)), "
        "min(agg(CONSULTING, out, period(2025-01-01, 2025-12-31), counterparty_in(related_parties)), "
        "const(300000)))"
    )
    assert "related_party_basket_capped" in [a["kind"] for a in alarms]
    assert spec["metric_text"] == _BASKET_METRIC  # исходный cellspec не мутирован


def test_apply_final_does_not_cap_basket_by_default():
    """Обратная совместимость: без новых kwarg-ов ничего не переписывается."""
    spec = {"metric_ast": parse(_BASKET_METRIC), "metric_text": _BASKET_METRIC}
    new, alarms = apply_final(spec, "любая непустая цитата")
    assert new["metric_text"] == _BASKET_METRIC
    assert alarms == []
