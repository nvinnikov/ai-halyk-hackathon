"""Офлайн-юниты extracted-режима solve: чистые функции сопоставления/сборки
cellspec и fail-open вокруг документного конвейера (задача 24, ревью раунда 1).

Полный прогон на LLM — в tests/test_extracted_run.py (маркер llm). Здесь —
то, что должен ловить make check без ключа: регресс в _match_clauses,
_extracted_cellspec и в деградации при сбое build_dossiers/_extracted_inputs.
"""

import json
from decimal import Decimal
from pathlib import Path

import solve
from dsl import parse, signature
from ledger import extract_archive, find_inputs, load_ledger, rows_of
from scindex import INDEX_VERSION, build_index
from stages import artifact
from templates import TEMPLATES, title_key
from util import workdir

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")


# --- _match_clauses -----------------------------------------------------------


def test_match_clauses_exact_match():
    mapping, unmatched = solve._match_clauses(["6.1", "6.2", "6.3"], ["6.1", "6.2", "6.3"])
    assert mapping == {"6.1": "6.1", "6.2": "6.2", "6.3": "6.3"}
    assert unmatched == []


def test_match_clauses_suffix_fallback_when_counts_equal():
    # Номера пунктов не совпадают целиком (другой раздел договора), но число
    # ячеек равно числу извлечённых пунктов — доматч по числовому суффиксу.
    mapping, unmatched = solve._match_clauses(["6.1", "6.2", "6.3"], ["7.1", "7.2", "7.3"])
    assert mapping == {"6.1": "7.1", "6.2": "7.2", "6.3": "7.3"}
    assert unmatched == []


def test_match_clauses_mixed_direct_and_suffix():
    # 6.1 матчится напрямую, 6.2/6.3 — по суффиксу с оставшимися пунктами.
    mapping, unmatched = solve._match_clauses(["6.1", "6.2", "6.3"], ["6.1", "7.2", "7.3"])
    assert mapping == {"6.1": "6.1", "6.2": "7.2", "6.3": "7.3"}
    assert unmatched == []


def test_match_clauses_suffix_fallback_survives_extra_covenant():
    # Ревью PR #9 (3-я волна): лишний извлечённый ковенант (промпт просит
    # «найди ВСЕ») не выключает доматч — от ложного матча защищает
    # однозначность суффикса, а не равенство счётчиков.
    mapping, unmatched = solve._match_clauses(["6.1", "6.2", "6.3"], ["7.1", "7.2", "7.3", "9.5"])
    assert mapping == {"6.1": "7.1", "6.2": "7.2", "6.3": "7.3"}
    assert unmatched == []


def test_match_clauses_missing_covenant_leaves_cell_unmatched():
    # Извлечено меньше, чем ячеек: непокрытая ячейка уходит в unmatched
    # (лестница), покрытые матчатся по однозначным суффиксам.
    mapping, unmatched = solve._match_clauses(["6.1", "6.2", "6.3"], ["7.1", "7.2"])
    assert mapping == {"6.1": "7.1", "6.2": "7.2"}
    assert unmatched == ["6.3"]


def test_match_clauses_ambiguous_suffix_stays_unmatched():
    # Оба извлечённых пункта имеют суффикс "1" — неоднозначность, обе целевые
    # ячейки остаются непокрытыми (алярм clause_unmatched выше по стеку).
    mapping, unmatched = solve._match_clauses(["6.1", "6.2"], ["7.1", "8.1"])
    assert mapping == {}
    assert sorted(unmatched) == ["6.1", "6.2"]


# --- _extracted_cellspec -------------------------------------------------------


def _spec(**over) -> dict:
    base = {
        "quote": "цитата пункта",
        "valid": True,
        "errors": [],
        "missing_doc_keys": [],
        "title_key": "неизвестный заголовок без совпадения",
        "template": None,
        "direction": "max",
        "limit": "100",
        "trigger": None,
        "metric": "agg(CAPEX, out)",
    }
    base.update(over)
    return base


def test_extracted_cellspec_missing_clause_is_lookup_error():
    cellspec_or_error, quote = solve._extracted_cellspec(None, "6.1")
    assert isinstance(cellspec_or_error, LookupError)
    assert quote == ""


def test_extracted_cellspec_invalid_spec_is_value_error_but_keeps_quote():
    sp = _spec(valid=False, errors=["quote_unverified"])
    cellspec_or_error, quote = solve._extracted_cellspec(sp, "6.1")
    assert isinstance(cellspec_or_error, ValueError)
    assert quote == sp["quote"]  # лестница эвристики читает цитату даже у невалидной спеки


def test_extracted_cellspec_heading_match_wins_over_template():
    # title_key совпадает с заголовком шаблона capex — исполняется
    # канонический DSL шаблона, даже если specs_extract матчил другую
    # сигнатуру (искусственно испорчена, чтобы отличить источник).
    # Категория извлечённой метрики совпадает с шаблонной: категорийное
    # расхождение — отдельный путь с откатом (тест ниже).
    heading = title_key("Максимальные расходы по категории")
    sp = _spec(title_key=heading, template="revenue", metric="agg(CAPEX, out, min_amount(10))")
    cellspec, quote = solve._extracted_cellspec(sp, "6.1")
    assert isinstance(cellspec, dict)
    assert cellspec["metric_text"] == TEMPLATES["capex"]
    assert quote == sp["quote"]


def test_extracted_cellspec_category_divergence_keeps_template_with_alarm():
    # Заголовок капекс-шаблона, извлечённая формула — про другую категорию:
    # шаблон всё равно исполняется (на публичном наборе такие расхождения —
    # ошибки извлечения синонимичных категорий, откат стоил −5.0 офлайн),
    # но расхождение обязано быть видно алярмом.
    # Роллап, а не лист: лист теперь параметризует категорию (тест ниже),
    # а синонимная путаница роллапов остаётся за шаблоном — как измерено.
    heading = title_key("Максимальные расходы по категории")
    sp = _spec(title_key=heading, template=None, metric="agg(OPEX_TOTAL, out)")
    cellspec, _quote = solve._extracted_cellspec(sp, "6.1")
    assert isinstance(cellspec, dict)
    assert cellspec["metric_text"] == TEMPLATES["capex"]
    kinds = [a["kind"] for a in cellspec["match_alarms"]]
    assert kinds == ["heading_category_divergence"]


def test_extracted_cellspec_stashes_shadow_metric_on_divergence():
    # Расхождение есть, шаблон победил — извлечённая формула обязана уцелеть
    # тенью: без неё run_cell нечего сравнивать, и подмена снова становится
    # видимой только текстом формулы.
    heading = title_key("Максимальные расходы по категории")
    sp = _spec(title_key=heading, template=None, metric="agg(OPEX_TOTAL, out)")
    cellspec, _quote = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["shadow_metric_text"] == "agg(OPEX_TOTAL, out)"


def test_shadow_set_when_signatures_agree_but_text_differs():
    """Тень ставится по факту подмены, а не по diverged.

    Регрессия на ревью PR #21, круг 2: signature() намеренно затирает знак
    Agg, поэтому пара net/out расхождением не считается — а знак прямо
    меняет actual. Раньше такая подмена шла молча; теперь тень есть, а от
    шума защищает changed_answer внутри _shadow_compare.
    """
    heading = title_key("Максимальные расходы по категории")
    sp = _spec(title_key=heading, template=None, metric="agg(CAPEX, net)")
    cellspec, _quote = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["metric_text"] == TEMPLATES["capex"]  # исполняется шаблон
    assert cellspec["shadow_metric_text"] == "agg(CAPEX, net)"
    # Сигнатуры равны — старое условие diverged тень бы не поставило.
    assert signature(parse("agg(CAPEX, net)")) == signature(parse(TEMPLATES["capex"]))


def test_loose_match_on_more_general_heading_computes_extracted_formula():
    """Пин приватного паттерна: заголовок БЕЗ уточняющего слова нестрого
    матчится на более специфичный шаблон, но исполняется извлечённая формула.

    «…капитальных затрат к EBITDA» (затраты самого заёмщика) уводит на
    group_capex_to_ebitda, «…рентабельность по EBITDA» (без «скорректированная»)
    — на adj_ebitda_margin. Обе подмены молча брали бы чужую метрику; их
    останавливают два независимых механизма, и тест держит оба: недостающий
    doc()-ключ шаблона откатывает без вето (вето — только у точного матча),
    а при живом ключе нестрогий матч отвергается по расхождению формул."""
    cases = [
        (
            "Максимальное отношение капитальных затрат к EBITDA",
            "ratio(agg(CAPEX, out), sub(agg(REVENUE, in), agg(OTHER_OPEX, out)))",
            "group_capex",
            "loose_heading_rejected_on_divergence",  # категорийное расхождение
        ),
        (
            "Минимальная рентабельность по EBITDA",
            "ratio(sub(agg(REVENUE, in), agg(OTHER_OPEX, out)), agg(REVENUE, in))",
            "ebitda_addbacks_material_total",
            "loose_heading_rejected_on_divergence",  # сигнатурное расхождение
        ),
    ]
    for heading, metric, doc_key, rejection in cases:
        sp = _spec(title_key=title_key(heading), metric=metric)
        # Ключа шаблона нет в фактах: откат по heading_doc_keys_missing,
        # вето derived_doc_key_missing у нестрогого матча не срабатывает.
        cellspec, _ = solve._extracted_cellspec(sp, "6.2", fact_keys=frozenset())
        assert isinstance(cellspec, dict) and cellspec["metric_text"] == metric
        # Ключ есть: шаблон доживает до сравнения формул и отвергается там.
        cellspec, _ = solve._extracted_cellspec(sp, "6.2", fact_keys=frozenset({doc_key}))
        assert isinstance(cellspec, dict) and cellspec["metric_text"] == metric
        kinds = [a["kind"] for a in cellspec["match_alarms"]]
        assert rejection in kinds and "heading_matched_loosely" in kinds


def test_category_template_takes_leaf_category_from_extracted_formula():
    """Шаблон «по категории»: категория — параметр пункта (боевой прогон:
    четыре пункта про «Маркетинговые расходы» считались запечённым CAPEX).
    Форму задаёт шаблон (знак его, фильтры не переносятся), категорию — тело
    пункта через извлечённую формулу; только лист таксономии."""
    heading = title_key("Максимальные расходы по категории")
    sp = _spec(title_key=heading, metric="agg(MARKETING, out, period(2025-01-01, 2025-12-31))")
    cellspec, _ = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["metric_text"] == "agg(MARKETING, out)"
    kinds = [a["kind"] for a in cellspec["match_alarms"]]
    assert "heading_category_parameterized" in kinds
    # Роллап — не статья: остаётся мерянное поведение «шаблон чинит синонимы».
    sp = _spec(title_key=heading, metric="agg(OPEX_TOTAL, out)")
    cellspec, _ = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["metric_text"] == TEMPLATES["capex"]
    # Не одиночный agg — форма другая, категория не извлекается.
    sp = _spec(title_key=heading, metric="ratio(agg(MARKETING, out), agg(REVENUE, in))")
    cellspec, _ = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["metric_text"] == TEMPLATES["capex"]
    # Совпадающая категория — обычная подмена шаблоном, без параметризации.
    sp = _spec(title_key=heading, metric="agg(CAPEX, out, min_amount(10))")
    cellspec, _ = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["metric_text"] == TEMPLATES["capex"]
    assert not any(a["kind"] == "heading_category_parameterized" for a in cellspec.get("match_alarms", []))
    # OTHER — корзина неразнесённого, не статья (ревью PR #26): базой ковенанта
    # не имеет права стать остаток нераспознанного, притом молча — после
    # подмены категории совпали бы и divergence-алярм не поднялся бы.
    sp = _spec(title_key=heading, metric="agg(OTHER, out)")
    cellspec, _ = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["metric_text"] == TEMPLATES["capex"]
    kinds = [a["kind"] for a in cellspec["match_alarms"]]
    assert "heading_category_divergence" in kinds
    assert "heading_category_parameterized" not in kinds
    # Несовместимый знак (ревью пост-мержа PR #26): доходный лист под
    # расходным шаблоном дал бы agg(REVENUE, out) — уверенный ноль мимо обоих
    # divergence-алярмов (категории совпали бы, signature() затирает знак).
    sp = _spec(title_key=heading, metric="agg(REVENUE, in)")
    cellspec, _ = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["metric_text"] == TEMPLATES["capex"]
    kinds = [a["kind"] for a in cellspec["match_alarms"]]
    assert "heading_category_divergence" in kinds
    assert "heading_category_parameterized" not in kinds
    # net совместим с любым шаблонным знаком — как в сигнатурном матче.
    sp = _spec(title_key=heading, metric="agg(MARKETING, net)")
    cellspec, _ = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["metric_text"] == "agg(MARKETING, out)"


def test_substituted_template_does_not_inherit_period_filter():
    """Пин отката переноса периода (PR #26, регрессия S2 на боевом прогоне):
    подмена шаблоном НЕ переносит period(...) извлечённой формулы. Единственная
    строка за границей года в приватном леджере — та, которую AUP включает в
    период по начислению; механический фильтр по дате давил документальное
    решение. Подмена остаётся видимой сигнатурным расхождением и тенью."""
    heading = title_key("Минимальная выручка по категории")
    sp = _spec(
        title_key=heading,
        direction="min",
        metric="agg(REVENUE, in, period(2025-01-01, 2025-12-31))",
    )
    cellspec, _ = solve._extracted_cellspec(sp, "6.2")
    assert cellspec["metric_text"] == TEMPLATES["revenue"]  # шаблон без фильтра
    kinds = [a["kind"] for a in cellspec["match_alarms"]]
    assert "heading_signature_divergence" in kinds
    assert cellspec["shadow_metric_text"] == sp["metric"]


def test_ebitda_reading_rewrites_extracted_formula():
    """Определение EBITDA из договора главнее извлечённой формулы (кейс J3 6.2
    боевого прогона): модель взяла роллап OPEX_TOTAL при договорном «за вычетом
    Операционных расходов» — EBITDA в минус на сотни миллионов. Переписывается
    только EBITDA-подвыражение, фильтры и знак узла сохраняются, подмена видна
    алярмом и тенью."""
    metric = (
        "ratio(agg(CONSULTING, out, period(2025-01-01, 2025-12-31)), "
        "sub(agg(REVENUE, in, period(2025-01-01, 2025-12-31)), "
        "agg(OPEX_TOTAL, out, period(2025-01-01, 2025-12-31))))"
    )
    sp = _spec(metric=metric, limit="0.20")
    cellspec, _ = solve._extracted_cellspec(sp, "6.2", ebitda_reading="line_item")
    assert "OTHER_OPEX" in cellspec["metric_text"] and "OPEX_TOTAL" not in cellspec["metric_text"]
    assert "period(2025-01-01, 2025-12-31)" in cellspec["metric_text"]  # фильтры целы
    alarm = next(a for a in cellspec["match_alarms"] if a["kind"] == "ebitda_definition_applied")
    assert alarm["reading"] == "line_item" and alarm["target"] == "metric"
    assert cellspec["shadow_metric_text"] == metric  # подмена видна тени


def test_ebitda_reading_rewrites_template_and_trigger():
    # Определение главнее и канона шаблонов: _EBITDA зашивает OTHER_OPEX, а
    # договор вправе выбрать второе прочтение (ebitda_total_opex).
    heading = title_key("Минимальный коэффициент покрытия процентов")
    sp = _spec(
        title_key=heading,
        direction="min",
        metric="ratio(sub(agg(REVENUE, in), agg(OTHER_OPEX, out)), agg(INTEREST, out))",
        trigger="gt(ratio(agg(FINANCING, in), sub(agg(REVENUE, in), agg(OTHER_OPEX, out))), const(3.0))",
    )
    cellspec, _ = solve._extracted_cellspec(sp, "6.1", ebitda_reading="all_opex")
    assert "OPEX_TOTAL" in cellspec["metric_text"]
    targets = {a["target"] for a in cellspec["match_alarms"] if a["kind"] == "ebitda_definition_applied"}
    assert targets == {"metric", "trigger"}
    # Триггер переписан в AST, не только в тексте алярма.
    from dsl import Agg as _Agg
    from dsl import walk as _walk

    trig_cats = {n.category for n in _walk(cellspec["trigger_ast"]) if isinstance(n, _Agg)}
    assert "OPEX_TOTAL" in trig_cats and "OTHER_OPEX" not in trig_cats


def test_ebitda_reading_noop_when_matching_or_absent():
    metric = "ratio(sub(agg(REVENUE, in), agg(OTHER_OPEX, out)), agg(INTEREST, out))"
    sp = _spec(metric=metric, direction="min")
    # Совпадающее прочтение — переписывать нечего, алярма нет.
    cellspec, _ = solve._extracted_cellspec(sp, "6.1", ebitda_reading="line_item")
    assert cellspec["metric_text"] == metric
    assert not any(a["kind"] == "ebitda_definition_applied" for a in cellspec.get("match_alarms", []))
    # Нет определения — поведение прежнее (fail-open).
    cellspec, _ = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["metric_text"] == metric
    # Голый роллап вне EBITDA-подвыражения не трогается: «доля в операционных
    # расходах» оперирует своей статьёй независимо от определения EBITDA.
    sp = _spec(metric="ratio(agg(CONSULTING, out), agg(OPEX_TOTAL, out))")
    cellspec, _ = solve._extracted_cellspec(sp, "6.1", ebitda_reading="line_item")
    assert cellspec["metric_text"] == "ratio(agg(CONSULTING, out), agg(OPEX_TOTAL, out))"


def test_resolve_echoes_limit_guard():
    """Эхо порога — двойной признак: значение равно порогу ячейки И источник
    цитаты не оправдывает факт. Прежний признак «цитата резолва внутри цитаты
    пункта» (ревью PR #26) вырождался на коротких цитатах: «$9,400,000.00» из
    полиса — подстрока цитаты пункта. Теперь оправдание по источнику
    (quote_outside_agreement от resolve_doc_fact): цитата, живущая вне
    договора и не живущая в договоре, — настоящий факт."""
    # Значение равно порогу, источник не оправдан (договор/неоднозначно) — эхо.
    assert solve._resolve_echoes_limit("9400000", "9400000", False)
    assert solve._resolve_echoes_limit(9400000, "9400000.00", False)
    assert solve._resolve_echoes_limit("-3.5", "3.5", False)  # модуль: порог без знака
    # Цитата атрибутирована вне договора — законное равенство, не эхо.
    assert not solve._resolve_echoes_limit("9400000", "9400000", True)
    # Значение не равно порогу — не эхо независимо от источника.
    assert not solve._resolve_echoes_limit("9400001", "9400000", False)
    assert not solve._resolve_echoes_limit("н/д", "9400000", False)
    assert not solve._resolve_echoes_limit("100", None, False)


def test_echo_guard_forgives_outside_quote_for_a_part_of_metric():
    assert not solve._resolve_echoes_limit(
        "250000", Decimal("250000"), quote_outside_agreement=True, whole_metric=False
    )


def test_echo_guard_is_unconditional_when_doc_is_the_whole_metric():
    assert solve._resolve_echoes_limit(
        "250000", Decimal("250000"), quote_outside_agreement=True, whole_metric=True
    )


def test_echo_guard_ignores_values_below_limit():
    assert not solve._resolve_echoes_limit(
        "249999", Decimal("250000"), quote_outside_agreement=True, whole_metric=True
    )


def test_extracted_cellspec_no_shadow_when_template_matches_extracted():
    # Формулы совпали — подменять нечего, тень не нужна: лишний проход по
    # леджеру ради заведомо равного значения не делаем.
    heading = title_key("Максимальные расходы по категории")
    sp = _spec(title_key=heading, template=None, metric=TEMPLATES["capex"])
    cellspec, _quote = solve._extracted_cellspec(sp, "6.1")
    assert "shadow_metric_text" not in cellspec


def _shadow_cellspec(shadow_metric: str, metric: str = "agg(CAPEX, out)") -> dict:
    return {
        "metric_ast": parse(metric),
        "metric_text": metric,
        "direction": "max",
        "limit": Decimal("100"),
        "trigger_ast": None,
        "shadow_metric_text": shadow_metric,
    }


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


def test_shadow_records_both_answers_and_alarms_when_they_differ():
    # Шаблон считает CAPEX (50 — COMPLIANT), извлечённая формула — TAX
    # (500 — BREACH). Ячейка остаётся шаблонной, но расхождение ОТВЕТА
    # обязано попасть и в трейс, и в alarms: только его читает run-report.
    # Улика теневой BREACH-ветки больше не null (новая политика evidence.find):
    # единственная читаемая строка TAX одновременно и переворачивающий
    # кандидат (её снятие даёт 0 <= порога), поэтому она — улика.
    rows = [_row("TXN-1", "CAPEX", "-50"), _row("TXN-2", "TAX", "-500")]
    cellspec = _shadow_cellspec("agg(TAX, out)")
    cell, trace = solve.run_cell("SC-S", "6.1", rows, {}, cellspec, [])
    assert cell["status"] == "COMPLIANT" and cell["actual"] == 50.0  # ячейку считает шаблон
    assert trace["shadow"] == {
        "metric": "agg(TAX, out)",
        "status": "BREACH",
        "actual": 500.0,
        "evidence_txn_id": "TXN-2",
        "changed_answer": True,
    }
    got = [a for a in trace["alarms"] if a["kind"] == "heading_divergence_changed_answer"]
    assert got and got[0]["scenario"] == "SC-S" and got[0]["clause"] == "6.1"


def test_shadow_catches_evidence_divergence_at_equal_value():
    """Улика — треть ответа, и она зависит от той же формулы.

    Регрессия на ревью PR #21, круг 3: evidence.find перебирает кандидатов
    через cellspec["metric_ast"], поэтому подмена двигает множество
    переворачивающих. Здесь обе формулы дают одинаковые BREACH и actual, но
    исключаемая связанная сторона входит только в одну из них — улика
    расходится, и это обязано поднять алярм.
    """
    facts = {
        "related_parties": ["Contoso"],
        "related_quotes": {"Contoso": "доля 51%"},
    }
    rows = [_row("TXN-1", "CAPEX", "-500"), _row("TXN-2", "TAX", "-500")]
    # Шаблон считает CAPEX и видит связанную сторону в кандидатах; тень
    # считает TAX, где та же сумма набрана строкой того же контрагента.
    cellspec = {
        "metric_ast": parse("agg(CAPEX, out, counterparty_in(related_parties))"),
        "metric_text": "agg(CAPEX, out, counterparty_in(related_parties))",
        "direction": "max",
        "limit": Decimal("100"),
        "trigger_ast": None,
        "shadow_metric_text": "agg(TAX, out, counterparty_in(related_parties))",
    }
    cell, trace = solve.run_cell("SC-E", "6.3", rows, facts, cellspec, [])
    assert cell["status"] == "BREACH" and cell["actual"] == 500.0
    sh = trace["shadow"]
    assert sh["status"] == "BREACH" and sh["actual"] == 500.0  # значение совпало
    assert sh["evidence_txn_id"] != cell["evidence_txn_id"]  # а улика — нет
    assert sh["changed_answer"] is True
    assert [a["kind"] for a in trace["alarms"] if isinstance(a, dict)] == [
        "heading_divergence_changed_answer"
    ]


def test_shadow_stays_silent_when_answers_agree():
    # Формулы разные, ответ один и тот же — расхождение ничего не стоило,
    # и алярма быть не должно: иначе в окне 19 строк шума вместо короткого
    # списка ячеек, которые правда надо смотреть.
    rows = [_row("TXN-1", "CAPEX", "-50"), _row("TXN-2", "TAX", "-50")]
    cellspec = _shadow_cellspec("agg(TAX, out)")
    _cell, trace = solve.run_cell("SC-S", "6.1", rows, {}, cellspec, [])
    assert trace["shadow"]["changed_answer"] is False
    assert not [a for a in trace.get("alarms", []) if a["kind"] == "heading_divergence_changed_answer"]


def test_shadow_failure_never_costs_the_cell():
    # Тень не считается (doc-ключа нет) — ячейка обязана остаться посчитанной
    # шаблоном, ошибка уходит в трейс и никуда больше.
    rows = [_row("TXN-1", "CAPEX", "-50")]
    cellspec = _shadow_cellspec("doc(missing_key)")
    cell, trace = solve.run_cell("SC-S", "6.1", rows, {"doc_facts": {}}, cellspec, [])
    assert cell["status"] == "COMPLIANT" and cell["actual"] == 50.0
    assert trace["tier"] == 0 and "error" in trace["shadow"]
    # Отказ тени обязан быть виден в run-report, иначе ноль по
    # heading_divergence_changed_answer неотличим от «тень не считалась»
    # (ревью PR #21, круг 4). Только alarms читают _alarm_counts.
    assert [a["kind"] for a in trace["alarms"] if isinstance(a, dict)] == ["shadow_failed"]


def test_shadow_failure_is_caught_structurally_not_by_luck(monkeypatch):
    """Падение ЛЮБОЙ строки тени не роняет ячейку во внешний except.

    Регрессия на ревью PR #21: раньше внутренний try покрывал только
    parse/compute, а q2 и print лежали снаружи — инвариант держался
    расположением строк. Ловит вызывающий, поэтому проверяем именно это:
    функция взрывается целиком, ячейка остаётся ярусом 0.
    """

    def boom(*_a, **_kw):
        raise RuntimeError("тень взорвалась целиком")

    monkeypatch.setattr(solve, "_shadow_compare", boom)
    rows = [_row("TXN-1", "CAPEX", "-50")]
    cellspec = _shadow_cellspec("agg(TAX, out)")
    cell, trace = solve.run_cell("SC-S", "6.1", rows, {}, cellspec, [])
    assert cell["status"] == "COMPLIANT" and cell["actual"] == 50.0
    assert trace["tier"] == 0 and trace["path"] == "dsl"
    assert "тень взорвалась целиком" in trace["shadow"]["error"]
    got = [a for a in trace["alarms"] if isinstance(a, dict) and a["kind"] == "shadow_failed"]
    assert got and got[0]["scenario"] == "SC-S" and got[0]["clause"] == "6.1"


def test_family_mismatch_detects_dollars_against_ratio_limit():
    # Доллары против «9.00x» на max-ковенанте — 189 тысяч раз, величины
    # разной природы, и кратное превышение здесь абсурдно.
    assert solve._family_mismatch(Decimal("1703882.44"), Decimal("9.00"), "max") is True


def test_family_mismatch_silent_on_homogeneous_pair():
    # Самое дальнее расхождение однородной пары на публичном наборе — ±45%.
    assert solve._family_mismatch(Decimal("8104772.36"), Decimal("6500000"), "max") is False
    assert solve._family_mismatch(Decimal("0.0411"), Decimal("0.04"), "max") is False


def test_family_mismatch_never_fires_below_the_limit():
    """Значение много меньше порога — законный ответ, а не промах семьёй.

    Регрессия на ревью PR #21, круг 1: нижняя ветвь guard'а била по
    комфортному соблюдению max-ковенанта, и разрыв там ничем не ограничен —
    пустая категория даёт сколь угодно большой. Ни один из этих случаев
    (200x, 13 тысяч, 133 тысячи — последний вплотную к настоящему промаху
    в 189 тысяч) не имеет права поднимать guard.
    """
    assert solve._family_mismatch(Decimal("0.0002"), Decimal("0.04"), "max") is False
    assert solve._family_mismatch(Decimal("150"), Decimal("2000000"), "max") is False
    assert solve._family_mismatch(Decimal("15"), Decimal("2000000"), "max") is False
    # Та же сторона у коэффициентного шаблона при долларовом пороге: ловить
    # её магнитудой нельзя, не задев строки выше.
    assert solve._family_mismatch(Decimal("0.33"), Decimal("1800000"), "max") is False


def test_family_mismatch_never_fires_on_min_covenant():
    """На min-ковенанте кратное превышение порога — соблюдение, не промах.

    Регрессия на ревью PR #21, круг 2: аргумент, которым убрана нижняя
    ветвь, симметрично применим к верхней на min. Покрытие процентов у
    заёмщика с крошечными процентными расходами уходит за порог в тысячи
    раз при совершенно верном шаблоне и верном значении — guard обязан
    молчать, иначе он выбросит правильный actual.
    """
    assert solve._family_mismatch(Decimal("20000"), Decimal("2.00"), "min") is False
    assert solve._family_mismatch(Decimal("2000"), Decimal("0.20"), "min") is False
    # Направление не прочиталось — отличить абсурд от комфорта нечем.
    assert solve._family_mismatch(Decimal("1703882.44"), Decimal("9.00"), None) is False


def test_family_mismatch_never_fires_on_zero_or_unknown_limit():
    # Ноль — законный ответ («таких операций не было»), подменять его порогом
    # значило бы терять верную ячейку; порога нет — сравнивать не с чем.
    assert solve._family_mismatch(Decimal("0"), Decimal("500000"), "max") is False
    assert solve._family_mismatch(Decimal("500000"), None, "max") is False
    assert solve._family_mismatch(Decimal("500000"), Decimal("0"), "max") is False


def _invalid_spec_error(limit: str, direction: str = "max") -> ValueError:
    err = ValueError("невалидная спека")
    err.spec_direction = direction  # type: ignore[attr-defined]
    err.spec_limit = Decimal(limit)  # type: ignore[attr-defined]
    return err


def test_heuristic_tier_keeps_limit_as_actual_on_family_mismatch():
    # Спека невалидна, эвристика по цитате даёт долларовый шаблон, а порог —
    # коэффициент: посчитанные доллары в actual не идут, остаётся порог.
    rows = [_row("TXN-1", "CAPEX", "-1703882.44")]
    cell, trace = solve.run_cell(
        "SC-M", "6.1", rows, {}, _invalid_spec_error("9.00"), [], quote="капитальные затраты Группы"
    )
    assert trace["tier"] == 1 and trace["template"] == "capex"
    assert cell["actual"] == 9.0
    kinds = [a["kind"] for a in trace["alarms"] if isinstance(a, dict)]
    assert "heuristic_family_mismatch" in kinds


def test_family_mismatch_does_not_condition_the_prior(monkeypatch):
    """Семья отвергнутого шаблона не имеет права выбирать ступень приора.

    Регрессия на ревью PR #21, круг 2: family_of считается по AST того же
    шаблона, который признан не той природы, и уходила в fallback_cell
    ключом direction|family. На публичном наборе замаскировано — до этой
    ступени prior_status не доходит, by_clause всегда попадает.
    """
    seen: dict = {}

    def spy(direction, family, limit, computed, clause=None):
        seen.update(direction=direction, family=family)
        return {"status": "BREACH", "actual": 0.0, "evidence_txn_id": None}, ["fallback_used"]

    monkeypatch.setattr(solve, "fallback_cell", spy)
    rows = [_row("TXN-1", "CAPEX", "-1703882.44")]
    solve.run_cell(
        "SC-M", "6.1", rows, {}, _invalid_spec_error("9.00"), [], quote="капитальные затраты Группы"
    )
    assert seen["direction"] == "max"
    assert seen["family"] is None  # не "absolute" от долларового шаблона


def test_heuristic_tier_still_uses_computed_actual_when_families_agree():
    # Однородная пара — поведение прежнее: actual берётся посчитанным.
    rows = [_row("TXN-1", "CAPEX", "-1652704.31")]
    cell, trace = solve.run_cell(
        "SC-M", "6.2", rows, {}, _invalid_spec_error("1800000"), [], quote="капитальные затраты"
    )
    assert trace["tier"] == 1 and cell["actual"] == 1652704.31
    kinds = [a["kind"] for a in trace["alarms"] if isinstance(a, dict)]
    assert "heuristic_family_mismatch" not in kinds


def test_with_doc_facts_keeps_model_total_when_no_addbacks():
    # Добавок не извлечено, модель дала итог numeric_fact'ом: ноль поверх
    # извлечённого числа — потеря данных, модельное значение остаётся
    # (ревью PR #9, 25-я волна).
    facts = {"ebitda_addbacks": [], "doc_facts": {"ebitda_addbacks_material_total": "500.00"}}
    out = solve._with_doc_facts(facts)
    assert out["doc_facts"]["ebitda_addbacks_material_total"] == "500.00"


def test_with_doc_facts_arithmetic_wins_when_addbacks_present():
    # Есть из чего считать — арифметика кода перебивает модельное значение.
    facts = {
        "ebitda_addbacks": ["100", "200"],
        "addback_materiality": "150",
        "doc_facts": {"ebitda_addbacks_material_total": "999"},
    }
    out = solve._with_doc_facts(facts)
    assert out["doc_facts"]["ebitda_addbacks_material_total"] == "200"


def test_run_cell_match_alarms_survive_dsl_fallback():
    # Спека с match_alarms есть, вычисление падает (doc-ключа нет) —
    # алярмы подмены не затираются fallback-путём (ревью PR #9, 25-я волна).
    cellspec = {
        "metric_ast": parse("doc(missing_key)"),
        "metric_text": "doc(missing_key)",
        "direction": "max",
        "limit": Decimal("100"),
        "trigger_ast": None,
        "match_alarms": [{"kind": "heading_signature_divergence", "extracted": "x", "template": "y"}],
    }
    _cell, trace = solve.run_cell("SC-Y", "9.8", [], {"doc_facts": {}}, cellspec, [])
    assert trace["tier"] == 2 and "dsl_error" in trace
    assert any(a.get("kind") == "heading_signature_divergence" for a in trace["alarms"])


def test_run_cell_match_alarms_reach_trace_alarms():
    # match_alarms обязаны доехать до общего trace["alarms"] — только его
    # читают _alarm_counts и invariants._collect_report_alarms; scenario и
    # clause внутри словаря спасают от глобального дедупа точных дублей.
    cellspec = {
        "metric_ast": parse("agg(CAPEX, out)"),
        "metric_text": "agg(CAPEX, out)",
        "direction": "max",
        "limit": Decimal("100"),
        "trigger_ast": None,
        "match_alarms": [{"kind": "heading_signature_divergence", "extracted": "x", "template": "y"}],
    }
    _cell, trace = solve.run_cell("SC-X", "9.9", [], {}, cellspec, [])
    got = [a for a in trace["alarms"] if a["kind"] == "heading_signature_divergence"]
    assert got and got[0]["scenario"] == "SC-X" and got[0]["clause"] == "9.9"


def test_extracted_cellspec_falls_back_to_template_signature():
    # Заголовок не совпал ни с одним шаблоном, но специфика уже посчитала
    # сигнатурный матч (match_signature) в sp["template"].
    sp = _spec(template="capex", metric="agg(CAPEX, out)")
    cellspec, _quote = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["metric_text"] == TEMPLATES["capex"]


def test_extracted_cellspec_falls_back_to_raw_metric():
    # Ни заголовок, ни сигнатура не совпали — исполняется сырой DSL спеки.
    raw_metric = "agg(TAX, out, min_amount(10))"
    sp = _spec(template=None, metric=raw_metric)
    cellspec, _quote = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["metric_text"] == raw_metric
    assert cellspec["metric_ast"] == parse(raw_metric)


def test_extracted_cellspec_malformed_limit_returns_exception_not_raises():
    sp = _spec(limit="not-a-number")
    cellspec_or_error, quote = solve._extracted_cellspec(sp, "6.1")
    assert isinstance(cellspec_or_error, Exception)
    assert not isinstance(cellspec_or_error, dict)
    assert quote == sp["quote"]


def test_extracted_cellspec_uses_decimal_limit_and_trigger():
    sp = _spec(limit="1.70", trigger="gt(agg(FINANCING, in), const(4000000))")
    cellspec, _quote = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["limit"] == Decimal("1.70")
    assert cellspec["trigger_ast"] is not None


# --- fail-open: сбой документного конвейера не роняет прогон ------------------


def _extracted_context() -> tuple[Path, Path, dict, list[str]]:
    """Тот же набор входов, что main() строит до документного конвейера —
    воспроизведён здесь, чтобы дёргать _extracted_inputs напрямую, в обход
    полного main()."""
    ds_hash, input_dir = extract_archive(PUBLIC_ZIP)
    wd = workdir(ds_hash)
    inputs = find_inputs(input_dir)
    template = json.loads(inputs["template"].read_text())
    targets = sorted(template["answers"])
    ledger_art = load_ledger(wd, input_dir, target_scenarios=targets)
    all_rows = rows_of(ledger_art)
    index = artifact(wd / "index.json", INDEX_VERSION, lambda: build_index(all_rows, targets))
    return wd, input_dir, index, targets


def test_dossier_build_failure_is_fail_open(monkeypatch):
    """Critical (ревью раунда 1): build_dossiers падает целиком — раньше это
    роняло _extracted_inputs, а с ним и main(), до записи скелета."""
    wd, input_dir, index, targets = _extracted_context()

    def boom(*a, **k):
        raise RuntimeError("искусственный сбой сшивки досье")

    monkeypatch.setattr(solve, "build_dossiers", boom)
    facts_by_sc, specs_by_sc = solve._extracted_inputs(wd, input_dir, index, targets)

    assert set(facts_by_sc) == set(targets)
    assert set(specs_by_sc) == set(targets)
    for sc in targets:
        assert specs_by_sc[sc]["clauses"] == {}
        assert any(a["kind"] == "dossier_build_failed" for a in specs_by_sc[sc]["alarms"])
        assert "doc_facts" in facts_by_sc[sc]  # факты прошли через _with_doc_facts


def test_extracted_inputs_failure_does_not_kill_main(monkeypatch):
    """Второй рубеж: даже если _extracted_inputs целиком отказала (баг, а не
    ожидаемый сбой документа) — main() дописывает submission по лестнице,
    а не падает с ненулевым кодом."""

    def boom(*a, **k):
        raise RuntimeError("искусственный сбой документного конвейера")

    monkeypatch.setattr(solve, "_extracted_inputs", boom)
    answers = solve.main(PUBLIC_ZIP, facts_source="extracted")
    for sc, cells in answers.items():
        for clause, cell in cells.items():
            assert sorted(cell) == ["actual", "evidence_txn_id", "status"], f"{sc} {clause}: {cell}"
            assert cell["status"] in ("BREACH", "COMPLIANT")
            assert isinstance(cell["actual"], int | float)


def test_specs_failure_keeps_extracted_facts(monkeypatch, tmp_path):
    """Ревью PR #9 (7-я волна): падение стадии спек не обнуляет уже посчитанные
    факты — fx_rates заёмщика остаются в донорском пуле."""
    import facts_extract as fe

    monkeypatch.setattr(solve, "find_inputs", lambda d: {"pdfs": []})
    monkeypatch.setattr(
        solve, "build_dossiers", lambda wd, pdfs, index, all_accounts=None: {"ACC-X": {"account_id": "ACC-X"}}
    )
    good_facts = {**fe._empty_facts(), "fx_rates": [{"currency": "EUR", "usd_per_unit": "1.1"}]}
    monkeypatch.setattr(solve, "extract_facts", lambda wd, d: dict(good_facts))

    def boom(*a, **k):
        raise RuntimeError("specs stage down")

    monkeypatch.setattr(solve, "extract_specs", boom)
    index = {"scenario_to_account": {"S1": "ACC-X"}}
    facts_by_sc, specs_by_sc = solve._extracted_inputs(tmp_path, tmp_path, index, ["S1"])
    assert facts_by_sc["S1"]["fx_rates"] == good_facts["fx_rates"]
    assert specs_by_sc["S1"]["clauses"] == {}
    assert specs_by_sc["S1"]["alarms"][0]["kind"] == "specs_failed"


def test_doc_facts_derivation_failure_is_per_borrower(monkeypatch, tmp_path):
    """NaN (или иной мусор), уроненный _with_doc_facts, стоит производных
    ключей одного заёмщика, а не извлечения всех 12 (ревью PR #9, 27-я
    волна): хвост цикла _extracted_inputs под пер-заёмщицким рубежом."""
    import facts_extract as fe

    monkeypatch.setattr(solve, "find_inputs", lambda d: {"pdfs": []})
    monkeypatch.setattr(
        solve, "build_dossiers", lambda wd, pdfs, index, all_accounts=None: {"ACC-X": {"account_id": "ACC-X"}}
    )
    bad_facts = {**fe._empty_facts(), "ebitda_addbacks": ["NaN"]}
    monkeypatch.setattr(solve, "extract_facts", lambda wd, d: dict(bad_facts))
    monkeypatch.setattr(solve, "extract_specs", lambda wd, d, keys: {"clauses": {}, "alarms": []})
    index = {"scenario_to_account": {"S1": "ACC-X"}}
    facts_by_sc, _specs = solve._extracted_inputs(tmp_path, tmp_path, index, ["S1"])
    assert any(a.get("kind") == "doc_facts_derivation_failed" for a in facts_by_sc["S1"]["alarms"])


def test_number_ok_rejects_nan_and_infinity():
    import facts_extract as fe

    assert not fe._number_ok("NaN")
    assert not fe._number_ok("Infinity")
    assert fe._number_ok("-1200000.50")


def test_resolve_failure_keeps_spec_art(monkeypatch, tmp_path):
    """Транзиентный сбой resolve_doc_fact стоит максимум своего doc-ключа:
    уже извлечённые спеки заёмщика не заменяются пустышкой specs_failed
    (ревью PR #9, 26-я волна)."""
    import facts_extract as fe

    monkeypatch.setattr(solve, "find_inputs", lambda d: {"pdfs": []})
    monkeypatch.setattr(
        solve, "build_dossiers", lambda wd, pdfs, index, all_accounts=None: {"ACC-X": {"account_id": "ACC-X"}}
    )
    monkeypatch.setattr(solve, "extract_facts", lambda wd, d: fe._empty_facts())
    spec_art = {
        "clauses": {
            "6.1": {
                "valid": False,
                "errors": [],
                "missing_doc_keys": ["insurance_min"],
                "quote": "страховое покрытие",
                "direction": "min",
                "limit": "1",
                "trigger": None,
                "metric": "doc(insurance_min)",
                "template": None,
                "title_key": "",
            }
        },
        "alarms": [],
    }
    monkeypatch.setattr(solve, "extract_specs", lambda wd, d, keys: spec_art)

    def resolve_boom(*a, **k):
        raise RuntimeError("gemini 429 storm")

    monkeypatch.setattr(solve, "resolve_doc_fact", resolve_boom)
    index = {"scenario_to_account": {"S1": "ACC-X"}}
    _facts_by_sc, specs_by_sc = solve._extracted_inputs(tmp_path, tmp_path, index, ["S1"])
    assert "6.1" in specs_by_sc["S1"]["clauses"]  # спеки уцелели
    assert not any(a.get("kind") == "specs_failed" for a in specs_by_sc["S1"]["alarms"])


def _loose_spec(title_key: str, metric: str) -> dict:
    return {
        "valid": True,
        "title_key": title_key,
        "metric": metric,
        "template": None,
        "direction": "max",
        "limit": "1",
        "trigger": None,
        "quote": "цитата пункта",
    }


def test_loose_heading_yields_to_extracted_dsl_on_divergence():
    """Нестрогий матч не получает кредит доверия точного.

    Библиотека двуязычна: законный шаблон бывает недостижим по словам, а
    сосед — достижим, и сходство с ним проходит и порог, и отрыв. Решение
    «шаблон исполняется и при расхождении» измерено на ТОЧНЫХ матчах, где
    заголовок гарантированно тот самый. Здесь посылка обратная, поэтому при
    расхождении считается извлечённая формула: цена ошибки — отсутствие
    матча, а не молча посчитанная чужая метрика.
    """
    from templates import TEMPLATES

    # Заголовок про долю платежей связанным сторонам в ВЫРУЧКЕ: в библиотеке он
    # зарегистрирован по-английски, поэтому по словам достижим только русский
    # брат — та же метрика, но со знаменателем по операционным расходам.
    sp = _loose_spec(
        title_key="максимальная доля платежей связанным сторонам в выручке",
        metric="ratio(agg(ALL, out, counterparty_in(related_parties)), agg(REVENUE, in))",
    )
    cellspec, _ = solve._extracted_cellspec(sp, "6.3", scenario="S1")
    kinds = [a["kind"] for a in cellspec.get("match_alarms", [])]
    assert "heading_matched_loosely" in kinds
    assert "loose_heading_rejected_on_divergence" in kinds
    assert cellspec["metric_text"] == sp["metric"], "расхождение обязано вернуть извлечённую формулу"
    assert cellspec["metric_text"] != TEMPLATES["related_share_opex"]


def test_loose_heading_keeps_template_when_formulas_agree():
    """Без расхождения нестрогий матч работает как задумано — шаблон исполняется."""
    from templates import TEMPLATES

    sp = _loose_spec(
        title_key="минимальный коэффициент покрытия",  # «процентов» выброшено
        metric=TEMPLATES["icr"],
    )
    cellspec, _ = solve._extracted_cellspec(sp, "6.1", scenario="S1")
    kinds = [a["kind"] for a in cellspec.get("match_alarms", [])]
    assert "heading_matched_loosely" in kinds
    assert "loose_heading_rejected_on_divergence" not in kinds
    assert cellspec["metric_text"] == TEMPLATES["icr"]


def test_exact_heading_still_executes_template_on_divergence():
    """Точный матч ведёт себя как раньше: шаблон исполняется, алярм остаётся."""
    from templates import TEMPLATE_HEADINGS, TEMPLATES

    key = next(k for k, name in TEMPLATE_HEADINGS.items() if name == "icr")
    sp = _loose_spec(title_key=key, metric="agg(REVENUE, in)")
    cellspec, _ = solve._extracted_cellspec(sp, "6.1", scenario="S1")
    kinds = [a["kind"] for a in cellspec.get("match_alarms", [])]
    assert "heading_matched_loosely" not in kinds
    assert "loose_heading_rejected_on_divergence" not in kinds
    assert cellspec["metric_text"] == TEMPLATES["icr"]


def test_derived_key_veto_applies_only_to_exact_heading_match():
    """Вето по производному ключу держится на посылке «заголовок опознан
    правильно». У нестрогого матча этой посылки нет: пункт про капзатраты
    самого заёмщика, нестрого сматчившийся на групповой шаблон, должен
    вернуться к извлечённой формуле, а не умереть (ревью PR #23, 10-я волна)."""
    from templates import _TEMPLATE_HEADING_TEXT, title_key

    heading = _TEMPLATE_HEADING_TEXT["group_capex_to_ebitda"]
    loose = title_key(" ".join(heading.split()[:-1]))  # то же, но без последнего слова
    sp = _spec(title_key=loose, metric="agg(CAPEX, out)")
    cellspec, _quote = solve._extracted_cellspec(sp, "6.1", fact_keys=frozenset())
    assert not isinstance(cellspec, Exception), "нестрогий матч не должен убивать ячейку"
    assert cellspec["metric_text"] == "agg(CAPEX, out)"


def test_derived_key_veto_still_fires_on_exact_heading_match():
    """При точном матче заголовок опознан, и извлечённая формула по леджеру
    считает другую величину — ячейка честно уходит на лестницу."""
    from templates import _TEMPLATE_HEADING_TEXT, title_key

    exact = title_key(_TEMPLATE_HEADING_TEXT["group_capex_to_ebitda"])
    sp = _spec(title_key=exact, metric="agg(CAPEX, out)")
    cellspec, quote = solve._extracted_cellspec(sp, "6.1", fact_keys=frozenset())
    assert isinstance(cellspec, ValueError)
    assert quote == "цитата пункта"
