"""Harness: скелет-первым submission, fail-open на ячейку, трейс.

Submission пишется задом наперёд (раздел 6): сначала на диск кладётся
полностью заполненный фолбэками скелет, каждая посчитанная ячейка
перезаписывает свою — на любой секунде прогона на диске валидный файл.
Скелет строится сразу после распаковки и чтения шаблона, до леджера и
индекса: всё, что может упасть после этого, оставляет на диске валидный
файл вместо пустоты.

Вычислительное ядро — лестница run_cell (5.7): спека в DSL → эвристика по
цитате пункта → приор; null в actual не существует как состояние. Источник
спек и фактов задаёт facts_source: "expected" — эталон (мост
legacy_spec_to_cellspec, регрессия/eval), "extracted" (дефолт, задача 24) —
документный конвейер: досье → факты → спеки, LLM трогает только чтение,
арифметика ковенанта — DSL и код."""

import dataclasses
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, "solution")
sys.path.insert(0, "eval")

import evidence
import facts_extract
import llm
import noise
import rewrites
from dossier import build_dossiers
from dsl import Agg, Doc, DslError, Ratio, Sub, parse, signature, unparse, validate, walk
from engine import agg, prepare_rows, select_rows, sign_divergence
from facts_extract import extract_facts, resolve_doc_fact, resolve_doc_metric
from fallbacks import fallback_cell, family_of, heuristic_template
from fx import coverage_alarms, to_usd
from ledger import dirty_rows_of, extract_archive, find_inputs, load_ledger, rows_of
from scindex import INDEX_VERSION, build_index
from specs_extract import extract_specs
from stages import artifact
from taxonomy import LEAVES, cell_other_alarm, coverage_report
from templates import (
    CATEGORY_PARAMETERIZED,
    TEMPLATE_HEADINGS,
    TEMPLATES,
    match_heading,
    match_signature,
)
from util import OUT, ROOT, q2, stable_json, workdir

# Модули, чьи *_VERSION-константы run-report собирает целиком (раздел 3):
# правка build-логики любой стадии обязана поднять версию — отчёт делает
# рассинхрон видимым, а не полагается на то, что его заметят при код-ревью.
_VERSIONED_MODULES = (
    "ledger",
    "scindex",
    "dossier",
    "route",
    "facts_extract",
    "specs_extract",
    "vision",
    "pdftext",
    "categorize_llm",
)


def _expected_facts() -> dict:
    """Эталонные факты из `eval/` — только для facts_source="expected".

    Импорт ленивый (ревью 8 августа): боевой extracted-путь не читает эталон
    вообще, а на верхнем уровне отсутствующий `eval/` ронял бы solve ещё до
    записи скелета — то есть в ноль ячеек вместо фолбэков. Заодно это та
    связь solution → eval, которую греп-гейт запрещает по смыслу.
    """
    from expected_extraction import FACTS

    return FACTS


def _expected_specs() -> dict:
    """Эталонные спеки из `eval/` — только для facts_source="expected", см. _expected_facts."""
    from expected_extraction import SPECS

    return SPECS


def submission_meta() -> dict:
    """Реквизиты отправки (раздел 3) — из env, а не хардкод: команда и
    контакт задаются перед боевым прогоном, модель по умолчанию — та, что
    реально зовёт llm.call (провайдер учитывается так же, как в
    _build_run_report — иначе gemini-прогон подписывался бы anthropic-моделью)."""
    default_model = llm.GEMINI_MODEL if llm._provider() == "gemini" else llm.MODEL
    meta = {
        "team": os.environ.get("TEAM_NAME", ""),
        "contact_email": os.environ.get("CONTACT_EMAIL", ""),
        "model": os.environ.get("MODEL_NAME", default_model),
    }
    for field in ("team", "contact_email"):
        if not meta[field]:
            # Забытый .env не роняет прогон (submission валиден и без
            # реквизитов), но и не молчит (ревью PR #9, 12-я волна).
            print(
                f"ALARM submission_meta_empty: {field} пуст — проверь TEAM_NAME/CONTACT_EMAIL в .env",
                flush=True,
            )
    return meta


# --- ядро на эталонных фактах (источник подменяется задачами 16/24) ----------


# Ключи doc_facts, которые вычисляет код из сырых фактов досье (_with_doc_facts
# и facts_extract._group_capex): адресный резолв (resolve_doc_fact) их не
# трогает — LLM не делает арифметику.
#
# group_capex тут не из осторожности, а по замеру: адресный резолв просит число
# по ОПИСАНИЮ из цитаты пункта, а цитата ковенанта группового уровня называет
# сам порог — модель его и возвращала. Порог, подставленный числителем, дал бы
# уверенное значение в тысячи раз меньше настоящего; отсутствие ключа отправляет
# ячейку на лестницу, и это заметно честнее.
_DERIVED_DOC_KEYS = frozenset({"ebitda_addbacks_material_total", "group_capex"})


def _with_doc_facts(facts: dict) -> dict:
    """doc_facts для doc()-метрик DSL: считается детерминированно из сырых
    фактов досье — арифметика (порог материальности, сумма добавок) остаётся
    в коде, LLM отдаёт только исходные числа. Производные ключи код ПЕРЕБИВАЕТ
    (не setdefault): модель, вернувшая ebitda_addbacks_material_total своим
    numeric_facts, не имеет права затенить арифметику (ревью PR #9, 16-я
    волна — та же дисциплина, что _DERIVED_DOC_KEYS для адресного резолва)."""
    out = dict(facts)
    doc_facts = dict(out.get("doc_facts", {}))
    addbacks = [Decimal(str(a)) for a in out.get("ebitda_addbacks", [])]
    materiality = Decimal(str(out.get("addback_materiality", 0)))
    derived_total = str(sum((a for a in addbacks if a >= materiality), Decimal(0)))
    model_value = doc_facts.get("ebitda_addbacks_material_total")
    if addbacks or model_value is None:
        # Арифметике есть из чего считаться (или модельного значения нет
        # вовсе) — производный ключ перебивается кодом, как и раньше.
        if model_value is not None and str(model_value) != derived_total:
            print(
                f"ALARM derived_doc_key_overridden: ebitda_addbacks_material_total "
                f"модели ({model_value}) заменён арифметикой кода ({derived_total})",
                flush=True,
            )
        doc_facts["ebitda_addbacks_material_total"] = derived_total
    else:
        # Добавок не извлечено, а модель значение дала: подстановка нуля
        # поверх извлечённого числа — не «арифметика перебивает», а потеря
        # данных (ревью PR #9, 25-я волна). Модельное значение остаётся,
        # расхождение видно алярмом.
        print(
            f"ALARM derived_doc_key_model_kept: ebitda_addbacks_material_total "
            f"модели ({model_value}) сохранён — ebitda_addbacks пуст, "
            f"арифметике не из чего считаться",
            flush=True,
        )
    if "severance_liability" in out:
        doc_facts.setdefault("severance_liability", str(out["severance_liability"]))
    out["doc_facts"] = doc_facts
    return out


def _facts_of(scenario: str) -> dict:
    """Факты досье сценария — единственная точка чтения FACTS.

    Задача 24 подменит здесь источник на извлечённые LLM факты, не трогая
    вызывающих (в том числе сбор донорских курсов по чужим заёмщикам).
    Факты всегда проходят через _with_doc_facts: doc()-метрики DSL ждут
    готовый doc_facts. Неизвестный сценарий (приватный набор) — пустые
    факты с алярмом, а не KeyError: расчёт по строкам без документальных
    решений лучше скелета.
    """
    facts = _expected_facts()
    if scenario not in facts:
        print(f"ALARM facts_missing {scenario}: расчёт без фактов досье", flush=True)
    return _with_doc_facts(facts.get(scenario, {}))


def load_rows(
    scenario: str, all_rows: list[dict], index: dict, facts: dict, donor_rates: list[dict]
) -> tuple[list, list, list]:
    """Строки сценария: отбор по счёту из индекса, курс, решения досье.

    Возвращает тройку (сырые, подготовленные, алярмы fx). Сырые нужны улике:
    она откатывает одно решение и пересобирает строки заново, а в
    подготовленных отсечённой операции уже нет.

    Порядок стадий обязателен: конвертация в USD идёт ДО prepare_rows,
    поэтому amount_override перекрывает уже сконвертированную сумму и курсом
    не домножается — записка казначейства фиксирует итоговую долларовую
    сумму (см. докстринг fx.py).
    """
    selected = select_rows(all_rows, index["scenario_to_account"][scenario])
    own_rates = facts.get("fx_rates", [])
    # Покрытие проверяется до расчёта: непокрытая пара (валюта, дата) — это
    # алярм уровня заёмщика, а не молчаливая потеря строки в агрегации.
    alarms = coverage_alarms(selected, own_rates, donor_rates)
    raw, row_alarms = to_usd(selected, own_rates, donor_rates)
    return raw, prepare_rows(raw, facts), alarms + row_alarms


def legacy_spec_to_cellspec(spec: tuple) -> dict:
    """Мост из легаси-кортежа SPECS в форму ячейки для интерпретатора.

    Временный: задача 24 будет собирать cellspec из извлечённой спеки."""
    name, direction, limit = spec[0], spec[1], spec[2]
    opts = spec[3] if len(spec) > 3 else {}
    trigger = None
    if "trigger_financing" in opts:
        trigger = parse(f"gt(agg(FINANCING, in), const({opts['trigger_financing']}))")
    return {
        "metric_ast": parse(TEMPLATES[name]),
        "metric_text": TEMPLATES[name],
        "direction": direction,
        "limit": Decimal(str(limit)),
        "trigger_ast": trigger,
    }


def _metric_categories(node) -> list[str]:
    """Расходные категории, читаемые метрикой, — из AST вместо ручного списка
    METRIC_CATEGORIES: тот вёлся руками при формулах-лямбдах и уехал бы от
    формул при первой правке."""
    return sorted({n.category for n in walk(node) if isinstance(n, Agg) and n.sign != "in"})


def _metric_filters(*nodes) -> dict[str, tuple[str, ...]]:
    """Имена фильтров по категориям Agg-узлов: алярм неразнесённых строк их
    не применяет, поэтому перечисляет в трейсе — иначе severity читалась бы
    как точная.

    Разбивка по категориям, а не общий список: пометка «охват нефильтрован»
    относится к конкретному слепому агрегату, и фильтр соседнего узла её не
    оправдывает. В related_share_revenue числитель читает ALL с
    counterparty_in, а слеп знаменатель agg(REVENUE, in) — без фильтров;
    общий список пометил бы ячейку нефильтрованной и отправил её в конец
    очереди разбора, прямо вопреки docstring taxonomy о законности такого
    алярма."""
    out: dict[str, set[str]] = {}
    for node in nodes:
        if node is None:
            continue
        for n in walk(node):
            if isinstance(n, Agg):
                out.setdefault(n.category, set()).update(type(f).__name__ for f in n.filters)
    return {cat: tuple(sorted(names)) for cat, names in sorted(out.items()) if names}


def _all_metric_categories(node) -> set[str]:
    """Все категории метрики, включая доходные: потерянная строка REVENUE —
    главный риск категоризации, а _metric_categories отбрасывает sign == in."""
    return {n.category for n in walk(node) if isinstance(n, Agg)}


def _metric_inputs(node, raw: list, facts: dict) -> dict:
    """Входы формулы для трейса: агрегат каждой пары (категория, знак) из AST."""
    rows = prepare_rows(raw, facts)
    return {
        f"{n.category}:{n.sign}": str(agg(rows, n.category, n.sign)) for n in walk(node) if isinstance(n, Agg)
    }


# Во сколько раз посчитанное значение должно ПРЕВЫСИТЬ порог, чтобы счесть их
# величинами разной природы. Промах эвристики, который мы ловим, — долларовый
# шаблон при коэффициентном пороге, то есть 10^6 против 10^0.
#
# Guard срабатывает ТОЛЬКО там, где кратный разрыв невозможен, а не там, где он
# всего лишь велик. Критерий один и применяется к обеим сторонам одинаково
# (ревью PR #21, два круга):
#
#   max-ковенант, значение НИЖЕ порога   — комфортное соблюдение, разрыв
#                                          неограничен (пустая категория);
#   max-ковенант, значение ВЫШЕ порога   — нарушение, и кратное превышение
#                                          однородной пары абсурдно;
#   min-ковенант, значение ВЫШЕ порога   — комфортное соблюдение, разрыв
#                                          неограничен (крошечный знаменатель:
#                                          покрытие процентов у заёмщика почти
#                                          без процентных расходов);
#   min-ковенант, значение НИЖЕ порога    — нарушение, но знаменатель тот же,
#                                          так что разрыв опять неограничен.
#
# Абсурдна ровно одна клетка из четырёх, поэтому guard живёт только в ней.
# Цена честная: промах семьёй на min-ковенанте и на нижней стороне max мы не
# ловим. Он стоит те же ≤0.50, что и ложное срабатывание, но в отличие от него
# гарантированно не портит верно посчитанную ячейку.
_FAMILY_MISMATCH_FACTOR = Decimal(10_000)


def _family_mismatch(value: Decimal, limit: Decimal | None, direction: str | None) -> bool:
    """Посчитанное значение НАСТОЛЬКО больше порога, что это величины разной
    природы?

    Только для max-ковенанта: на min кратное превышение порога — это
    соблюдение, а не признак промаха (см. таблицу выше). Неизвестное
    направление считается небезопасным: не зная, с какой стороны порога
    лежит норма, отличить абсурд от комфорта нечем.

    Ноль исключён по той же причине: относительное сравнение на нём не
    определено, а ноль — законный ответ («таких операций не было»), и
    подменять его порогом значило бы терять верную ячейку ради защиты от
    неверной.
    """
    if direction != "max" or limit is None or limit <= 0 or value <= 0:
        return False
    return value > limit * _FAMILY_MISMATCH_FACTOR


def _shadow_compare(
    trace: dict,
    cellspec: dict,
    raw: list,
    facts: dict,
    scenario: str,
    clause: str,
    status: str,
    res,
    ev_txn: str | None,
    quote: str = "",
) -> None:
    """Теневой расчёт извлечённой формулы, подменённой шаблоном.

    Диагностика, а не расчёт: ячейку по-прежнему считает шаблон, значение
    тени в submission не попадает. Своего try здесь нет намеренно — ловит
    вызывающий, и потому инвариант «тень не может стоить ячейки» держится
    структурой вызова, а не тем, что все опасные строки оказались внутри
    внутреннего try (ревью PR #21).

    Зачем. Решение «шаблон исполняется и при расхождении» измерено и остаётся
    (откат стоил −5.0 офлайн-скора), но текст двух формул не отвечает на
    единственный вопрос, который в окне важен: изменила ли подмена ответ.
    Совпал ответ — расхождение ничего не стоило и смотреть нечего; разошёлся —
    ячейку надо сверить глазами, и алярм называет её поимённо.

    Ответ — это ВСЯ тройка ячейки, включая улику (ревью PR #21, круг 3):
    evidence.find перебирает кандидатов через cellspec["metric_ast"], поэтому
    подмена формулы двигает и множество переворачивающих. Базовое значение
    двух формул может совпасть до второго знака, а «ровно один
    переворачивающий» — разойтись, и весь вес улики прошёл бы молча.
    Улика считается только при BREACH — при COMPLIANT find сразу отдаёт None.

    Строки и факты уже в памяти, сеть не нужна, цена — проход по леджеру плюс
    по контрфактуалу на кандидата.
    """
    shadow_text = cellspec.get("shadow_metric_text")
    if not shadow_text:
        return
    shadow_cs = {**cellspec, "metric_ast": parse(shadow_text), "metric_text": shadow_text}
    # Обе стороны сравнения проходят ОДНИ И ТЕ ЖЕ финальные переписывания
    # (ревью финальной ветки, §3). cellspec сюда приходит уже после
    # apply_final, а извлечённая формула — сырой из спеки, и без этого вызова
    # тень отвечала бы на другой вопрос: «изменили ли ответ подмена И
    # переписывания вместе», приписывая подмене заголовка чужое расхождение.
    # Ошибка была односторонней только по видимости: сузили одну сторону —
    # алярм врёт именем, сузили обе — молчит честно.
    shadow_cs, _rw = rewrites.apply_final(
        shadow_cs,
        quote,
        shadow_cs.get("direction"),
        shadow_cs.get("ebitda_needs_addback", False),
        doc_facts=facts.get("doc_facts"),
        doc_fact_quotes=facts.get("doc_fact_quotes"),
    )
    shadow_status, shadow_res = evidence.compute(raw, facts, shadow_cs)
    shadow_actual = q2(abs(shadow_res.value))
    shadow_txn, _ = evidence.find(raw, facts, shadow_cs, shadow_status)
    actual = q2(abs(res.value))
    changed = shadow_status != status or shadow_actual != actual or shadow_txn != ev_txn
    trace["shadow"] = {
        "metric": shadow_text,
        "status": shadow_status,
        "actual": shadow_actual,
        "evidence_txn_id": shadow_txn,
        "changed_answer": changed,
    }
    # Ключ появляется только тогда, когда переписывания тень действительно
    # тронули: иначе форма блока в трейсе менялась бы на всех ячейках ради
    # повторения metric.
    if shadow_cs.get("metric_text") != shadow_text:
        trace["shadow"]["metric_computed"] = shadow_cs.get("metric_text")
    if not changed:
        return
    alarm = {
        "kind": "heading_divergence_changed_answer",
        "template": {"status": status, "actual": actual, "evidence_txn_id": ev_txn},
        "extracted": {
            "status": shadow_status,
            "actual": shadow_actual,
            "evidence_txn_id": shadow_txn,
        },
    }
    # scenario/clause внутрь словаря — иначе глобальный дедуп точных дублей в
    # _alarm_counts схлопнул бы одинаковые расхождения разных ячеек в «1».
    trace.setdefault("alarms", []).append({**alarm, "scenario": scenario, "clause": clause})
    print(f"ALARM heading_divergence_changed_answer {scenario} {clause}: {alarm}", flush=True)


def _metric_equals_limit(cellspec: dict, facts: dict) -> bool:
    """Метрика ячейки — ГОЛЫЙ doc(ключ), и его значение по модулю в точности
    равно порогу ячейки: тавтология, а не измерение.

    Отличие от `_resolve_echoes_limit`: тот гасит эхо НА ВХОДЕ, в одной точке
    резолва одного doc-ключа. Этот гард — по факту тавтологии НА ВЫХОДЕ,
    равнодушный к тому, как значение попало в doc_facts (адресный резолв,
    общий проход фактов, любой будущий источник); он ловит не одну лазейку, а
    сам исход — метрика ячейки тождественно равна порогу.

    Условие узкое нарочно: узел обязан быть ГОЛЫМ Doc, не частью Add/Sub/Ratio/
    Agg — слагаемое, равное порогу, мыслимо законно (полис ровно на нужную
    сумму), и это не его случай. Равенство — точное, не приближённое: иначе
    гард начнёт бить законные ответы впритык."""
    metric_ast = cellspec.get("metric_ast")
    if not isinstance(metric_ast, Doc):
        return False
    raw_value = facts.get("doc_facts", {}).get(metric_ast.key)
    if raw_value is None:
        return False
    try:
        return abs(Decimal(str(raw_value))) == abs(Decimal(str(cellspec["limit"])))
    except (InvalidOperation, TypeError, ValueError, KeyError):
        return False


def _fallback_ladder(
    scenario: str,
    clause: str,
    quote: str,
    spec_direction,
    spec_limit,
    raw: list,
    facts: dict,
    computed: list,
    trace: dict,
) -> tuple[dict, dict]:
    """Эвристика по цитате → приор. Общий хвост лестницы (5.7): направление и
    порог уже прочитаны (спекой или доносятся с ошибки), дальше решает либо
    шаблон по ключевым словам цитаты, либо глобальный/семейный приор.

    Вынесено из run_cell (ревью пост-приватного набора, раунд 1): у «спека
    невалидна» и «спека валидна, но метрика — тавтология» разные входы, но
    один и тот же хвост — дублировать его было дороже, чем параметризовать."""
    tpl = heuristic_template(quote)
    if tpl is not None:
        try:
            metric_ast = parse(TEMPLATES[tpl])
            # Эвристика даёт метрику; статус берёт приор (направление/семья —
            # из невалидной спеки, если прочитались), actual — посчитанное.
            _, res = evidence.compute(
                raw,
                facts,
                {"metric_ast": metric_ast, "direction": "max", "limit": Decimal(0), "trigger_ast": None},
            )
            value = abs(res.value)
            mismatched = _family_mismatch(value, spec_limit, spec_direction)
            # Семья считается по AST ЭТОГО ЖЕ шаблона, поэтому признанный
            # промахом шаблон не имеет права кондиционировать ею приор: она
            # заведомо не та, и ступень by[направление|семья] увела бы статус
            # по чужой статистике (ревью PR #21). На публичном наборе это
            # замаскировано — prior_status до неё не доходит, by_clause всегда
            # попадает; на приватном номер пункта может и не найтись. None —
            # честный глобальный приор.
            #
            # Осторожно при чтении отчёта: пометка fallback_coin_flip, которой
            # приор себя при этом клеймит, приходит из fallback_cell СТРОКОЙ, а
            # _alarm_kind считает видом только dict с "kind" — в run-report она
            # неотличима от прочего мусора в "other" (ревью PR #21, круг 5).
            # Видно её только в trace["alarms"] конкретной ячейки. Перевод пары
            # fallback_used/fallback_coin_flip на словари — правка вне этого
            # PR и не в окно: она сдвинет счётчик "other", на который ранбук
            # ссылается ориентиром.
            family = None if mismatched else family_of(metric_ast, spec_limit)
            cell, alarms = fallback_cell(spec_direction, family, spec_limit, computed, clause=clause)
            if mismatched:
                # Эвристика по ключевым словам цитаты угадала не ту СЕМЬЮ:
                # шаблон меряет доллары, а порог — коэффициент. Порог от
                # fallback_cell остаётся: он хотя бы того же порядка, что
                # искомое значение, а посчитанное — заведомо не оно.
                # Словарём, а не строкой: _alarm_kind считает видом только
                # dict с "kind", строка ушла бы в общий мусорный "other" и в
                # run-report была бы неразличима. scenario/clause внутрь —
                # от глобального дедупа точных дублей.
                mismatch = {
                    "kind": "heuristic_family_mismatch",
                    "scenario": scenario,
                    "clause": clause,
                    "value": str(value),
                    "limit": str(spec_limit),
                }
                alarms = alarms + [mismatch]
                print(f"ALARM heuristic_family_mismatch {scenario} {clause}: {mismatch}", flush=True)
            else:
                cell["actual"] = q2(value)
            # Мерж, не присваивание: trace может уже нести alarms с более
            # раннего рубежа (rewrite/match_alarms/metric_equals_limit) —
            # присваивание стёрло бы их (тот же класс бага, что ревью PR #9,
            # 25-я волна, но здесь для heuristic-ветки, а не dsl-исключения).
            trace.update(
                path="heuristic_template", tier=1, template=tpl, alarms=trace.get("alarms", []) + alarms
            )
            return cell, trace
        except Exception as exc:
            trace["heuristic_error"] = repr(exc)
    cell, alarms = fallback_cell(spec_direction, None, spec_limit, computed, clause=clause)
    trace.update(path="prior", tier=2, alarms=trace.get("alarms", []) + alarms)
    return cell, trace


def run_cell(
    scenario: str,
    clause: str,
    raw: list,
    facts: dict,
    cellspec_or_error,
    computed: list,
    quote: str = "",
) -> tuple[dict, dict]:
    """Лестница целиком: спека → эвристика по цитате → приор. (ячейка, трейс).

    Ярус пишется в trace["tier"]: 0 — dsl, 1 — heuristic_template, 2 — prior;
    его читает инвариант check_fallback_rate (задача 26). Модуль значения
    берётся только при записи в ячейку: вердикт считается со знаком.
    quote — цитата пункта договора; задача 24 передаёт её из извлечённой
    спеки, в expected-режиме она пуста."""
    trace: dict = {"scenario": scenario, "clause": clause, "quote": quote}
    if isinstance(cellspec_or_error, dict):
        # Переписывания — улучшение, а не условие расчёта, и стоят они ДО
        # общего try ниже: без собственного перехвата их сбой (обход AST,
        # unparse нового корня, регулярки по цитате) улетал бы во внешний
        # except main, где ячейка — ещё скелет. Это мимо лестницы: прочитанные
        # направление и порог выбрасываются, actual теряет порог, приор теряет
        # семью. Инвариант fail-open требует обратного — сбой стоит яруса, а
        # не прочитанного (ревью финальной ветки, §2). Считаем исходную спеку.
        try:
            cellspec, rewrite_alarms = rewrites.apply_final(
                cellspec_or_error,
                quote,
                cellspec_or_error.get("direction"),
                cellspec_or_error.get("ebitda_needs_addback", False),
                doc_facts=facts.get("doc_facts"),
                doc_fact_quotes=facts.get("doc_fact_quotes"),
            )
        except Exception as exc:
            cellspec, rewrite_alarms = cellspec_or_error, []
            trace["rewrite_error"] = repr(exc)
            print(f"ALARM rewrite_failed {scenario} {clause}: {exc!r}", flush=True)
        for alarm in rewrite_alarms:
            trace.setdefault("alarms", []).append({**alarm, "scenario": scenario, "clause": clause})
            print(f"ALARM {alarm['kind']} {scenario} {clause}: {alarm}", flush=True)
        trace["spec"] = {
            "quote": quote,
            "direction": cellspec["direction"],
            "limit": str(cellspec["limit"]),
            "metric": cellspec.get("metric_text", ""),
        }
        for alarm in cellspec.get("match_alarms", []):
            trace.setdefault("match_alarms", []).append(alarm)
            # И в общий alarms трейса: сканеры run-report (_alarm_counts) и
            # invariants._collect_report_alarms читают только alarms/fx_alarms
            # — без этого подмена метрики шаблоном не видна в run-report
            # (ревью PR #9, 21-я волна). scenario/clause внутрь словаря: иначе
            # одинаковое расхождение у разных ячеек схлопнулось бы глобальным
            # дедупом точных дублей до «1».
            trace.setdefault("alarms", []).append({**alarm, "scenario": scenario, "clause": clause})
            print(f"ALARM {alarm['kind']} {scenario} {clause}: {alarm}")
        if _metric_equals_limit(cellspec, facts):
            # Метрика ячейки — голый doc(ключ), и он тождественно равен
            # порогу: не измерение, тавтология «впритык соблюдено». Спека
            # валидна формально, но обрабатывается как невалидная — лестница
            # фолбэков решает статус приором, а не уверенным вердиктом на
            # нулевом ярусе (ревью по приватному набору, раунд 1).
            equal_alarm = {
                "kind": "metric_equals_limit",
                "scenario": scenario,
                "clause": clause,
                "limit": str(cellspec["limit"]),
            }
            trace.setdefault("alarms", []).append(equal_alarm)
            print(f"ALARM metric_equals_limit {scenario} {clause}: {equal_alarm}", flush=True)
            return _fallback_ladder(
                scenario, clause, quote, cellspec["direction"], cellspec["limit"], raw, facts, computed, trace
            )
        try:
            status, res = evidence.compute(raw, facts, cellspec)
            ev_txn, ev_trace = evidence.find(raw, facts, cellspec, status)
            trace.update(
                path="dsl",
                tier=0,
                formula=cellspec.get("metric_text", ""),
                inputs=_metric_inputs(cellspec["metric_ast"], raw, facts),
                value=str(res.value),
                evidence=ev_trace,
                flags=sorted(res.flags),
            )
            cell = {"status": status, "actual": q2(abs(res.value)), "evidence_txn_id": ev_txn}
            try:
                _shadow_compare(trace, cellspec, raw, facts, scenario, clause, status, res, ev_txn, quote)
            except Exception as shadow_exc:
                # Ячейка уже собрана и остаётся ярусом 0: диагностика не имеет
                # права уронить расчёт во внешний except и заменить посчитанное
                # приором. Свой except именно здесь, а не внутри функции, —
                # тогда инвариант структурный (ревью PR #21).
                trace["shadow"] = {
                    "metric": cellspec.get("shadow_metric_text", ""),
                    "error": repr(shadow_exc),
                }
                # И отдельным алярмом (ревью PR #21, круг 4): run-report читает
                # только alarms/fx_alarms, поэтому молча упавшая тень делала бы
                # ноль по heading_divergence_changed_answer неотличимым от «ни
                # одна подмена не изменила ответ». Ранбук называет эту строку
                # главной, значит её ноль обязан значить ровно то, что написан.
                failed = {
                    "kind": "shadow_failed",
                    "scenario": scenario,
                    "clause": clause,
                    "error": repr(shadow_exc),
                }
                trace.setdefault("alarms", []).append(failed)
                print(f"ALARM shadow_failed {scenario} {clause}: {failed}", flush=True)
            return cell, trace
        except Exception as exc:
            # Спека построилась, вычисление упало: направление и порог прочитаны,
            # лестница не выбрасывает их (5.7) — actual = порог, статус — приор.
            trace["dsl_error"] = repr(exc)
            cell, alarms = fallback_cell(
                cellspec["direction"],
                family_of(cellspec["metric_ast"], cellspec["limit"]),
                cellspec["limit"],
                computed,
                clause=clause,
            )
            # Мерж, не присваивание: в trace["alarms"] уже могут лежать
            # match_alarms подмены шаблоном (ревью PR #9, 25-я волна —
            # update() затирал их на пути «спека есть, вычисление упало»).
            trace.update(path="prior", tier=2, alarms=trace.get("alarms", []) + alarms)
            return cell, trace
    trace["spec_error"] = repr(cellspec_or_error)
    # 5.7: прочитанные направление и порог невалидной спеки не выбрасываются —
    # _extracted_cellspec вешает их на ошибку, лестница доносит до приора
    # (ревью PR #9, 6-я волна).
    spec_direction = getattr(cellspec_or_error, "spec_direction", None)
    spec_limit = getattr(cellspec_or_error, "spec_limit", None)
    return _fallback_ladder(scenario, clause, quote, spec_direction, spec_limit, raw, facts, computed, trace)


def _donor_rates(targets: list[str], scenario: str, facts_of) -> list[dict]:
    """Донорская ступень лестницы: курсы всех прочих целевых заёмщиков.

    facts_of — функция сценарий → факты досье: expected-режим читает эталон
    (_facts_of), extracted — уже посчитанный facts_by_sc.get. Порядок доноров
    фиксирован, чтобы выбор не зависел от порядка сценариев в шаблоне;
    окончательный тай-брейк — в fx.pick_rate."""
    return sorted(
        (r for other in targets if other != scenario for r in facts_of(other).get("fx_rates", [])),
        key=lambda r: (r.get("doc_date") or "", r.get("doc_hash") or "", str(r.get("usd_per_unit"))),
    )


def _extracted_facts_of(wd: Path, index: dict, scenario: str) -> dict:
    """Извлечённые факты сценария из артефакта на диске (fail-open).

    Read-only аналог _facts_of для extracted-режима: артефакты facts/<ACC>
    уже построены прогоном solve.main, LLM не зовётся. Нет артефакта или
    счёта — пустые факты: инвариант честнее посчитать по строкам без
    документальных решений, чем на эталоне, которого 9 августа не будет."""
    acc = index["scenario_to_account"].get(scenario)
    try:
        raw_facts = json.loads((wd / "facts" / f"{acc}.json").read_text())
    except Exception:
        print(f"ALARM facts_missing {scenario}: расчёт без фактов досье", flush=True)
        raw_facts = facts_extract._empty_facts()
    return _with_doc_facts(raw_facts)


def scenario_inputs(archive: Path, scenario: str, facts_source: str = "expected") -> tuple[list[dict], dict]:
    """Строки заёмщика после fx-конвертации + факты, факты уже пропущены
    через _with_doc_facts. Источник фактов задаёт facts_source: "expected" —
    эталон, "extracted" — артефакты документного конвейера с диска (ревью
    PR #9, 3-я волна: пер-заёмщицкие инварианты на приватном наборе обязаны
    смотреть на то, что реально считал прогон, а не на несуществующий
    эталон). Переиспользуется тестами и eval-скриптами; повторный вызов
    дёшев — стадии кэшируются в work/<hash>.

    В отличие от main, скелет submission здесь не строится: это read-only
    вход для парити-теста, контрфактуалов задачи 16 и инвариантов."""
    archive = Path(archive)
    ds_hash, input_dir = extract_archive(archive)
    wd = workdir(ds_hash)
    # Голый вызов намеренно (ревью PR #25): рубеж со записью скелета живёт
    # только в main. Здесь он не защищает, а рискует — эта функция никогда не
    # является входом run.sh, её зовут парити-тест, контрфактуалы и
    # eval/invariants.py, причём последний оборачивает в isolated_solve_out
    # только solve.main. Запись отсюда шла бы в БОЕВОЙ out/ и заменяла бы
    # посчитанный submission пустым скелетом — ровно то, ради чего изоляция и
    # ставилась.
    inputs = find_inputs(input_dir)
    template = json.loads(inputs["template"].read_text())
    targets = sorted(template["answers"])
    ledger_art = load_ledger(wd, input_dir, target_scenarios=targets)
    all_rows = rows_of(ledger_art)
    index = artifact(wd / "index.json", INDEX_VERSION, lambda: build_index(all_rows, targets))
    scenario_rows = all_rows + dirty_rows_of(ledger_art)
    if facts_source == "extracted":
        facts_of = lambda sc: _extracted_facts_of(wd, index, sc)  # noqa: E731
    else:
        facts_of = _facts_of
    facts = facts_of(scenario)
    raw, _rows, _alarms = load_rows(
        scenario, scenario_rows, index, facts, _donor_rates(targets, scenario, facts_of)
    )
    return raw, facts


def _extracted_inputs(
    wd: Path, input_dir: Path, index: dict, targets: list[str]
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Документный конвейер: досье → факты → спеки, всё артефактами.

    Сбой на одном заёмщике (в том числе исчерпание LLM-бюджета — llm.
    BudgetExhausted это тоже Exception) не имеет права остановить прогон до
    записи уже посчитанных ячеек: заёмщик получает пустые факты и алярм
    extraction_failed, его ячейки уходят по лестнице run_cell, остальные
    заёмщики считаются как обычно.

    build_dossiers сама по себе fail-open по документу и по заёмщику (задача
    24, ревью раунда 1), но защита не абсолютна (например find_inputs может
    упасть до маршрутизации) — здесь дополнительный рубеж: полный отказ
    построить досье не должен утащить прогон, все заёмщики уходят на пустые
    факты вместо падения main() до записи скелета."""
    facts_by_sc: dict[str, dict] = {}
    specs_by_sc: dict[str, dict] = {}
    try:
        pdfs = find_inputs(input_dir)["pdfs"]
        # all_accounts (целевые + фоновые) — иначе ветка background_document в
        # route недостижима и каждый фоновый PDF шумел бы routing_quarantine
        # (ревью PR #9, 13-я волна).
        all_accounts = sorted(
            set(index["scenario_to_account"].values())
            | set(index.get("background", {}).get("account_ids", []))
        )
        dossiers = build_dossiers(wd, pdfs, index, all_accounts)
    except Exception as exc:
        print(f"ALARM dossier_build_failed: {exc!r}", flush=True)
        for sc in targets:
            facts_by_sc[sc] = _with_doc_facts(facts_extract._empty_facts())
            specs_by_sc[sc] = {
                "clauses": {},
                "alarms": [{"kind": "dossier_build_failed", "error": repr(exc)}],
            }
        return facts_by_sc, specs_by_sc
    for sc in targets:
        acc = index["scenario_to_account"].get(sc)
        if acc is None:
            # индекс не связал сценарий со счётом: пустые факты, ячейки уйдут по лестнице
            facts_by_sc[sc] = _with_doc_facts(facts_extract._empty_facts())
            specs_by_sc[sc] = {"clauses": {}, "alarms": []}
            continue
        try:
            facts = extract_facts(wd, dossiers[acc])
        except Exception as exc:
            print(f"ALARM extraction_failed {sc}: {exc!r}", flush=True)
            facts_by_sc[sc] = _with_doc_facts(facts_extract._empty_facts())
            specs_by_sc[sc] = {
                "clauses": {},
                "alarms": [{"kind": "extraction_failed", "scenario": sc, "error": repr(exc)}],
            }
            continue
        # Спеки — отдельным fail-open (ревью PR #9, 7-я волна): их падение не
        # имеет права обнулить уже посчитанные факты — иначе fx_rates заёмщика
        # выпадают из донорского пула и строки в непокрытой валюте выбывают
        # у ДРУГИХ заёмщиков.
        try:
            # fact_keys для валидации спек — от УЖЕ обогащённых фактов
            # (ревью PR #9, 4-я волна): производные ключи считает код в
            # _with_doc_facts, и спека с doc(<производный ключ>) обязана
            # видеть его существующим — иначе она неисправимо невалидна,
            # а резолв запрещён (_DERIVED_DOC_KEYS: LLM не делает арифметику).
            spec_art = extract_specs(wd, dossiers[acc], set(_with_doc_facts(facts)["doc_facts"]))
            for _cl, sp in sorted(spec_art["clauses"].items()):
                for key in sp.get("missing_doc_keys", []):
                    if key in _DERIVED_DOC_KEYS:
                        # Производный ключ считает КОД из сырых фактов
                        # (_with_doc_facts); адресный LLM-резолв не имеет
                        # права затенить арифметику (ревью PR #9, 3-я волна).
                        continue
                    try:
                        resolved = resolve_doc_fact(wd, dossiers[acc], key, sp["quote"])
                    except Exception as exc:
                        # Транзиентный сбой резолва (бюджет, сеть, конфигурация
                        # провайдера) стоит максимум этого doc-ключа, не всех
                        # спек заёмщика: без локального рубежа исключение
                        # долетало до внешнего except и заменяло уже
                        # полученный валидный spec_art пустым — три ячейки на
                        # приоре на ровном месте (ревью PR #9, 26-я волна).
                        print(f"ALARM doc_fact_resolve_error {sc} {_cl} {key}: {exc!r}", flush=True)
                        resolved = None
                    metric_is_this_doc = False
                    try:
                        metric_ast = parse(sp["metric"])
                        metric_is_this_doc = isinstance(metric_ast, Doc) and metric_ast.key == key
                    except DslError:
                        metric_is_this_doc = False
                    if resolved is not None and _resolve_echoes_limit(
                        resolved["value"],
                        sp.get("limit"),
                        resolved.get("quote_outside_agreement", False),
                        whole_metric=metric_is_this_doc,
                    ):
                        # Мерянный на group_capex паттерн, обобщённый на
                        # произвольный ключ: адресный резолв просит число по
                        # описанию из цитаты пункта, а цитата называет сам
                        # порог — модель возвращает его. Такое «значение»
                        # делает метрику равной порогу и даёт уверенный ложный
                        # вердикт впритык; честнее лестница с эвристикой по
                        # цитате. Признак эха двойной: равенство порогу И
                        # источник цитаты (resolve_doc_fact атрибутирует её по
                        # документам досье) — законное равенство из полиса,
                        # процитированное вне договора, гард не трогает. Но
                        # только если doc-ключ — часть метрики: когда он и
                        # есть вся метрика ячейки (metric_is_this_doc), это
                        # оправдание не действует — тавтология статуса не
                        # бывает законной.
                        print(
                            f"ALARM doc_fact_resolve_echoes_limit {sc} {_cl}: {key}",
                            flush=True,
                        )
                        resolved = None
                    if resolved is not None:
                        facts["doc_facts"][key] = resolved["value"]
                        facts["doc_fact_quotes"][key] = resolved["quote"]
                    else:
                        # Тихих отбросов нет (ревью PR #9, 8-я волна): причина
                        # будущего фолбэка ячейки видна в момент отброса.
                        print(f"ALARM doc_fact_unresolved {sc} {_cl}: {key}", flush=True)
            # Перепроверка с пополненными doc_facts: extract_specs гоняет
            # _check при каждом чтении (задача 23), повторного похода к
            # модели не требует.
            spec_art = extract_specs(wd, dossiers[acc], set(_with_doc_facts(facts)["doc_facts"]))
            # Ярус после числового резолва: ключи, так и не нашедшиеся числом,
            # пробуются формулой по леджеру. Подстановка живёт в памяти прогона
            # и полностью детерминирована LLM-кэшем — артефакт спек на диске
            # не мутируется.
            spec_art = _resolve_ledger_doc_metrics(
                wd, dossiers[acc], spec_art, set(_with_doc_facts(facts)["doc_facts"]), sc
            )
        except Exception as exc:
            print(f"ALARM specs_failed {sc}: {exc!r}", flush=True)
            spec_art = {
                "clauses": {},
                "alarms": [{"kind": "specs_failed", "scenario": sc, "error": repr(exc)}],
            }
        # Определение EBITDA — отдельным fail-open рубежом: его сбой (в том
        # числе CassetteMiss в офлайне) не имеет права уронить уже извлечённые
        # спеки в specs_failed. Нет определения — ключа нет, формулы не
        # переписываются, поведение прежнее.
        #
        # Раньше здесь стояло «на публичной кассете этого вызова нет», и это
        # было правдой: офлайн-прогон промахивался мимо кассеты всегда, то есть
        # регрессия постоянно ходила по ветке fail-open. После волны починок
        # версия стадии фактов поднялась, живые прогоны переизвлекли факты, и
        # ответы легли в кассету — ветка fail-open перестала исполняться в
        # регрессионных прогонах. Разбор: docs/ops/private-set-postmortem.md,
        # раздел про поведение LLM-слоя.
        if spec_art.get("clauses"):
            try:
                ebitda_def = facts_extract.ebitda_definition(wd, dossiers[acc])
            except Exception as exc:
                print(f"ALARM ebitda_definition_error {sc}: {exc!r}", flush=True)
                ebitda_def = None
            if ebitda_def is not None:
                # Копия, не мутация: spec_art может быть только что записанным
                # артефактом, дописывать в его словарь — играть с диском.
                spec_art = {**spec_art, "ebitda_reading": ebitda_def}
        try:
            facts_by_sc[sc] = _with_doc_facts(facts)
        except Exception as exc:
            # Пер-заёмщицкий рубеж и на хвосте цикла (ревью PR #9, 27-я
            # волна): NaN, пролезший в факты с прогретого артефакта, ронял
            # InvalidOperation из _with_doc_facts мимо всех try — и внешний
            # catch-all main() обнулял извлечение всем 12 заёмщикам вместо
            # одного. Сырые факты лучше пустых: doc()-метрики уйдут лестницей,
            # остальное посчитается.
            print(f"ALARM doc_facts_derivation_failed {sc}: {exc!r}", flush=True)
            facts_by_sc[sc] = {
                **facts,
                "doc_facts": dict(facts.get("doc_facts", {})),
                "alarms": facts.get("alarms", [])
                + [{"kind": "doc_facts_derivation_failed", "scenario": sc, "error": repr(exc)}],
            }
        specs_by_sc[sc] = spec_art
    return facts_by_sc, specs_by_sc


def _resolve_ledger_doc_metrics(wd, dossier_art: dict, spec_art: dict, fact_keys: set, scenario: str) -> dict:
    """Формульный резолв невалидных спек: doc-ключ метрики → агрегат леджера.

    Второй ярус после числового резолва (resolve_doc_fact): величина вроде
    «выплат тела долга» не названа числом ни в одном документе, потому что
    это агрегат по леджеру. Модель по цитате пункта выписывает формулу DSL
    (facts_extract.resolve_doc_metric валидирует её грамматикой, таксономией
    и запретом констант — эхо порога исключено синтаксически), а здесь
    doc(<ключ>) в метрике заменяется этим выражением текстуально: doc-вызов
    атомарен, замена вызова вызовом сохраняет грамматику. Ключ, не найденный
    в тексте метрики (иная форма записи), — отказ, не риск.

    Работает только по спекам, невалидным ИСКЛЮЧИТЕЛЬНО из-за недостающих
    doc-ключей: прочие ошибки (quote_unverified, limit_not_in_quote) означают,
    что метрике нельзя верить целиком, и чинить её точечно нечестно. Любой
    отказ на любом шаге — прежнее поведение, лестница; починенная спека
    объявляется поимённо (doc_key_resolved_as_formula)."""
    clauses = dict(spec_art.get("clauses", {}))
    changed = False
    for cl, sp in sorted(clauses.items()):
        if sp.get("valid") or sp.get("errors") or not sp.get("missing_doc_keys"):
            continue
        metric = sp["metric"]
        resolved_all = True
        for key in sorted(sp["missing_doc_keys"]):
            try:
                expr = resolve_doc_metric(wd, dossier_art, key, sp.get("quote", ""))
            except Exception as exc:
                print(f"ALARM doc_metric_resolve_error {scenario} {cl} {key}: {exc!r}", flush=True)
                expr = None
            if expr is None:
                resolved_all = False
                break
            substituted = re.sub(rf"doc\(\s*'?{re.escape(key)}'?\s*\)", expr, metric)
            if substituted == metric:
                resolved_all = False
                break
            metric = substituted
        if not resolved_all:
            continue
        try:
            node = parse(metric)
        except DslError:
            continue
        still_missing = sorted({n.key for n in walk(node) if isinstance(n, Doc)} - set(fact_keys))
        if still_missing or [e for e in validate(node, set(fact_keys)) if "doc-ключ" not in e]:
            continue
        print(
            f"ALARM doc_key_resolved_as_formula {scenario} {cl}: {sorted(sp['missing_doc_keys'])}",
            flush=True,
        )
        clauses[cl] = {
            **sp,
            "metric": metric,
            "missing_doc_keys": [],
            "valid": True,
            "template": match_signature(node),
        }
        changed = True
    return {**spec_art, "clauses": clauses} if changed else spec_art


def _resolve_echoes_limit(
    value, limit, quote_outside_agreement: bool = False, whole_metric: bool = False
) -> bool:
    """Резолв вернул порог самой ячейки, взяв его из текста договора, — эхо.

    Признака два, и нужны оба: равенство порогу по модулю (порог в цитате
    печатается без знака) И отсутствие оправдания по источнику цитаты.
    Прежний второй признак — вхождение цитаты резолва в цитату пункта (ревью
    PR #26) — вырождался на коротких цитатах: цитата-число из полиса — это
    подстрока цитаты пункта, и гард бил законное равенство. Теперь источник
    определяет resolve_doc_fact пословной верификацией по каждому документу
    досье: цитата, живущая вне договора и не живущая ни в одном договоре, —
    настоящий факт, не эхо (полис ровно на требуемую сумму). Неоднозначный
    источник (голое число в обоих текстах, сшитая цитата) остаётся эхом —
    цена этой ошибки ограничена статусом, обратная — уверенным вердиктом
    впритык. Неразбираемое значение или отсутствующий порог — не эхо.

    Оправдание по источнику цитаты не действует, когда doc-ключ — ВСЯ метрика
    ячейки: величина, тождественно равная порогу, не измеряет ничего, она
    делает вердикт «впритык соблюдено» независимо от данных. Законное
    совпадение полиса с порогом мыслимо для слагаемого, но не для метрики
    целиком; на приватном наборе этот путь стоил трёх ячеек, и все три —
    ложный COMPLIANT."""
    try:
        if abs(Decimal(str(value))) != abs(Decimal(str(limit))):
            return False
    except (InvalidOperation, TypeError, ValueError):
        return False
    if whole_metric:
        return True
    return not quote_outside_agreement


def _clause_suffix(clause: str) -> str:
    return clause.rsplit(".", 1)[-1]


def _match_clauses(target_clauses: list[str], extracted_keys: list[str]) -> tuple[dict[str, str], list[str]]:
    """Сопоставление ячеек шаблона извлечённым номерам пунктов (правка 4).

    Основной путь — точное совпадение номера пункта (ключи spec_art["clauses"]
    уже нормализованы specs_extract). Оставшиеся ячейки доматчиваются по
    числовому суффиксу (последний сегмент после точки: пункт из другого
    раздела договора матчится ячейкой с тем же порядковым номером в
    подпункте). От ложного матча защищает ОДНОЗНАЧНОСТЬ суффикса (ровно один
    кандидат), а не равенство счётчиков: промпт сам просит «найди ВСЕ
    ковенанты», и лишний извлечённый пункт не должен отправлять весь
    заёмщик на приор (ревью PR #9, третья волна). Непокрытые ячейки
    возвращаются вторым элементом — алярм clause_unmatched, ячейка уходит
    по лестнице."""
    extracted_set = set(extracted_keys)
    mapping: dict[str, str] = {t: t for t in target_clauses if t in extracted_set}
    remaining = [t for t in target_clauses if t not in mapping]
    if remaining:
        by_suffix: dict[str, list[str]] = {}
        for e in sorted(extracted_set - set(mapping.values())):
            by_suffix.setdefault(_clause_suffix(e), []).append(e)
        still: list[str] = []
        for t in remaining:
            candidates = by_suffix.get(_clause_suffix(t), [])
            if len(candidates) == 1:
                mapping[t] = candidates[0]
                by_suffix[_clause_suffix(t)] = []
            else:
                still.append(t)
        remaining = still
    return mapping, remaining


def _metric_text_for(sp: dict, scenario: str, hide_templates: frozenset) -> str:
    """Текст метрики для спеки: библиотека шаблонов или сырой DSL (LOBO, 7.3).

    Для сценариев из hide_templates библиотека отключается целиком — и
    match_heading, и сигнатурный матч, — считается сырая формула из спеки:
    так LOBO ловит шаблон, подогнанный под конкретного заёмщика."""
    if sp.get("template") and scenario not in hide_templates:
        return TEMPLATES[sp["template"]]
    return sp["metric"]


def _category_divergence(extracted_text: str, template_text: str) -> tuple[list, list] | None:
    """Наборы категорий двух метрик, если они различаются (иначе None).

    Нужен для видимости риска heading-матча: заголовок пункта не всегда
    кодирует категорию, а канонический DSL шаблона кодирует её жёстко.
    """
    try:
        cats = lambda text: sorted({n.category for n in walk(parse(text)) if isinstance(n, Agg)})  # noqa: E731
        a, b = cats(extracted_text), cats(template_text)
    except DslError:
        return None
    return (a, b) if a != b else None


def _parameterize_category(extracted_text: str, template_text: str) -> str | None:
    """Категория извлечённой формулы в шаблоне «по категории» (или None).

    Заголовок «расходы/выручка по категории» делает категорию параметром
    пункта: на публичном наборе она всегда совпадала с запечённой в шаблон, на
    чужом — статья называется в теле пункта, и шаблон обязан взять её оттуда
    (боевой прогон: четыре пункта про «Маркетинговые расходы» считались как
    CAPEX). Форму по-прежнему задаёт шаблон: знак его, фильтры извлечённой
    формулы не переносятся. Берётся только ЛИСТ таксономии: роллап
    (OPEX_TOTAL, ALL) — не статья, а мерянная синонимная путаница извлечения,
    которую как раз чинит шаблон. Не одиночный agg — другая форма, категорию
    из него не извлечь."""
    try:
        ext, tpl = parse(extracted_text), parse(template_text)
    except DslError:
        return None
    if not isinstance(tpl, Agg) or tpl.filters or not isinstance(ext, Agg):
        return None
    if ext.category == tpl.category or ext.category not in LEAVES:
        return None
    if ext.category == "OTHER":
        # OTHER — корзина неразнесённого, а не статья (ревью PR #26): пункт
        # про статью вне таксономии дал бы базой ковенанта остаток
        # нераспознанного, причём после подмены категории совпали бы и
        # heading_category_divergence уже не поднялся бы.
        return None
    if ext.sign not in (tpl.sign, "net"):
        # Несовместимый знак (ревью пост-мержа PR #26): доходный лист под
        # расходным шаблоном — agg(REVENUE, in) под «расходами по категории» —
        # дал бы agg(REVENUE, out), то есть уверенный ноль на max-ковенанте,
        # причём мимо обоих divergence-алярмов: категории после подмены
        # совпадают, а signature() затирает знак. net совместим с любым
        # шаблонным знаком — как в сигнатурном матче. Отказ — прежний путь:
        # шаблон + heading_category_divergence + тень.
        return None
    return f"agg({ext.category}, {tpl.sign})"


def _apply_ebitda_reading(text: str, reading: str) -> str | None:
    """Текст формулы с категорией опекса по определению EBITDA из договора;
    None — переписывать нечего (или текст не парсится).

    Определение договора главнее и извлечённой формулы, и канона шаблонов:
    «EBITDA означает Выручку за вычетом Операционных расходов» — это статья
    (лист OTHER_OPEX), «за вычетом всех операционных расходов» — роллап
    OPEX_TOTAL (ebitda_total_opex — второе легитимное прочтение). Переписывается
    ТОЛЬКО EBITDA-подвыражение — sub(выручка, опекс), — а не всякий agg опекса:
    ковенант «доля консультационных в операционных расходах» оперирует своей
    статьёй независимо от определения EBITDA. Знак и фильтры узла сохраняются."""
    target = "OTHER_OPEX" if reading == "line_item" else "OPEX_TOTAL"
    wrong = "OPEX_TOTAL" if target == "OTHER_OPEX" else "OTHER_OPEX"
    try:
        ast = parse(text)
    except DslError:
        return None

    changed = False

    def rewrite(node):
        nonlocal changed
        if isinstance(node, Sub) and isinstance(node.a, Agg) and isinstance(node.b, Agg):
            if node.a.category == "REVENUE" and node.b.category == wrong:
                changed = True
                return Sub(a=node.a, b=dataclasses.replace(node.b, category=target))
            return node
        if not hasattr(node, "__dataclass_fields__"):
            return node
        updates = {}
        for name in node.__dataclass_fields__:
            value = getattr(node, name)
            if isinstance(value, tuple):
                rebuilt = tuple(rewrite(c) if hasattr(c, "__dataclass_fields__") else c for c in value)
                if rebuilt != value:
                    updates[name] = rebuilt
            elif hasattr(value, "__dataclass_fields__"):
                rebuilt = rewrite(value)
                if rebuilt != value:
                    updates[name] = rebuilt
        return dataclasses.replace(node, **updates) if updates else node

    rewritten = rewrite(ast)
    return unparse(rewritten) if changed else None


def _extracted_cellspec(
    sp: dict | None,
    clause: str,
    scenario: str = "",
    hide_templates: frozenset = frozenset(),
    fact_keys: frozenset | None = None,
    ebitda_reading: str | None = None,
    ebitda_needs_addback: bool = False,
) -> tuple[object, str]:
    """Cellspec-или-ошибка + цитата пункта из извлечённой спеки (правка 3).

    Матч реализации: сначала заголовок пункта (match_heading — основной
    путь, 19 заголовков однозначно определяют метрику), при промахе —
    DSL-сигнатура (sp["template"], её уже посчитал specs_extract), при
    промахе — сырой DSL спеки. Невалидная/ненайденная спека → ошибка,
    лестница run_cell подхватывает цитату для эвристики. hide_templates
    (LOBO, 7.3) глушит и match_heading, и сигнатурный матч для указанного
    сценария — резолвится только текст спеки без библиотеки."""
    if sp is None:
        return LookupError(f"clause {clause} не найден в договоре"), ""
    quote = sp.get("quote", "")
    if not sp["valid"]:
        err = ValueError(f"невалидная спека: {sp['errors'] or sp['missing_doc_keys']}")
        # 5.7: направление и порог прочитаны и не выбрасываются — едут на
        # ошибке до лестницы run_cell (ревью PR #9, 6-я волна).
        try:
            err.spec_direction = sp["direction"]  # type: ignore[attr-defined]
            err.spec_limit = Decimal(sp["limit"])  # type: ignore[attr-defined]
        except (InvalidOperation, KeyError, TypeError):
            pass
        return err, quote
    try:
        matched = match_heading(sp["title_key"])
        # Нестрогий матч — заголовок опознан ПРИБЛИЗИТЕЛЬНО, и доверие к нему
        # другое, чем к точному (см. ниже откат при расхождении).
        loosely_matched = matched is not None and sp["title_key"] not in TEMPLATE_HEADINGS
        template = matched or sp["template"]
        metric_text = _metric_text_for({**sp, "template": template}, scenario, hide_templates)
        # Подмена шаблоном не имеет права вводить doc()-ключи, которых нет в
        # фактах (ревью PR #9, 13-я волна): _check валидировал ключи
        # ИЗВЛЕЧЁННОЙ формулы, а шаблон может требовать свой — KeyError в
        # evaluate уронил бы валидную ячейку на приор. При недостающем ключе —
        # откат на извлечённый DSL с алярмом.
        if fact_keys is not None and metric_text != sp["metric"]:
            tpl_doc_keys = {n.key for n in walk(parse(metric_text)) if isinstance(n, Doc)}
            missing_tpl = sorted(tpl_doc_keys - set(fact_keys))
            if missing_tpl:
                print(
                    f"ALARM heading_doc_keys_missing {scenario} {clause}: {missing_tpl}",
                    flush=True,
                )
                derived_missing = sorted(set(missing_tpl) & _DERIVED_DOC_KEYS)
                if derived_missing and not loosely_matched:
                    # Производный ключ считает КОД из документов досье. Не
                    # посчитал — значит документа нет, и извлечённая формула его
                    # не заменяет: она берёт число из леджера, а это другая
                    # величина, не приближение к недостающей. Откат сюда,
                    # задуманный как страховка от KeyError на валидной ячейке,
                    # дал бы уверенно посчитанный не тот ответ; отсутствие
                    # ответа честнее (ревью PR #23, вторая волна).
                    #
                    # Только при ТОЧНОМ матче (ревью PR #23, десятая волна). Весь
                    # довод держится на посылке «заголовок опознан правильно, и
                    # ковенант действительно про величину из документа», а у
                    # нестрогого матча этой посылки нет — там мы заголовок как
                    # раз не узнали. Пункт про капзатраты самого заёмщика,
                    # нестрого сматчившийся на групповой шаблон, вето убило бы,
                    # хотя ниже нестрогий матч был бы отвергнут по расхождению и
                    # посчиталась бы извлечённая формула, для него верная.
                    print(
                        f"ALARM derived_doc_key_missing {scenario} {clause}: {derived_missing}",
                        flush=True,
                    )
                    err = ValueError(f"невалидная спека: {derived_missing}")
                    try:
                        err.spec_direction = sp["direction"]  # type: ignore[attr-defined]
                        err.spec_limit = Decimal(sp["limit"])  # type: ignore[attr-defined]
                    except (InvalidOperation, KeyError, TypeError):
                        pass
                    return err, quote
                metric_text = sp["metric"]
        # Перенос period(...) извлечённой формулы в шаблон здесь ПРОБОВАЛИ и
        # откатили (PR #26, регрессия S2 на боевом прогоне): единственная
        # строка за границей календарного года во всём приватном леджере —
        # ровно та, которую AUP-отчёт включает в ковенантный период по методу
        # начисления. Механический фильтр по дате задавил документальное
        # решение и обнулил базу выручки. Исключения из периода выражаются
        # ярусом фактов (excluded_txns), а не фильтром формулы.
        match_alarms: list[dict] = []
        # Категория — параметр для шаблонов «по категории». Только строгий
        # матч: у нестрогого посылки «заголовок тот самый» нет, и его защита
        # отказом по расхождению не ослабляется.
        if metric_text != sp["metric"] and not loosely_matched and template in CATEGORY_PARAMETERIZED:
            param = _parameterize_category(sp["metric"], metric_text)
            if param is not None:
                metric_text = param
                match_alarms.append({"kind": "heading_category_parameterized", "metric": param})
        # Подмена извлечённого DSL шаблоном остаётся (ruling: шаблон при матче
        # исполняется), но обязана быть видимой ЦЕЛИКОМ (ревью PR #9, 10-я
        # волна): не только другой набор категорий, но и разница по
        # quarter()/знаку/форме агрегации — сигнатурное сравнение.
        diverged = False
        if metric_text != sp["metric"]:
            div = _category_divergence(sp["metric"], metric_text)
            if div:
                diverged = True
                match_alarms.append(
                    {"kind": "heading_category_divergence", "extracted": div[0], "template": div[1]}
                )
                # Шаблон исполняется и при категорийном расхождении — решение
                # измерено, а не выбрано вкусом (ревью PR #9, 21-я волна
                # предлагала откат на извлечённый DSL): на публичном наборе
                # все такие расхождения — ошибки ИЗВЛЕЧЕНИЯ синонимичных
                # категорий (OPEX_TOTAL против OTHER_OPEX в EBITDA, OTHER
                # против ALL у related-party), шаблон их чинит, и откат стоил
                # −5.0 балла офлайн-скора. Алярм остаётся и доезжает до
                # run-report — на приватном наборе расхождения видны сразу.
            else:
                try:
                    if signature(parse(sp["metric"])) != signature(parse(metric_text)):
                        diverged = True
                        match_alarms.append(
                            {
                                "kind": "heading_signature_divergence",
                                "extracted": sp["metric"],
                                "template": metric_text,
                            }
                        )
                except DslError:
                    pass  # сырой DSL не парсится — подмена и так единственный путь
        if loosely_matched:
            match_alarms.append(
                {
                    "kind": "heading_matched_loosely",
                    "title_key": sp["title_key"],
                    "template": matched,
                }
            )
            # Решение «шаблон исполняется и при расхождении» измерено на ТОЧНЫХ
            # матчах, где заголовок гарантированно тот самый, а расхождение —
            # ошибка извлечения категорий. У нестрогого матча посылка обратная:
            # заголовок мы как раз не узнали, и расхождение может означать, что
            # увело на соседний шаблон. Библиотека двуязычна (часть заголовков
            # английские), поэтому законный шаблон бывает недостижим по словам,
            # а русский брат — достижим: сходство с ним проходит и порог, и
            # отрыв. Тогда откат на извлечённый DSL стоит нам отсутствия матча,
            # то есть ровно того, что было до нестрогого матча, — а исполнение
            # соседнего шаблона стоило бы неверной формулы, посчитанной молча.
            if diverged:
                match_alarms.append(
                    {
                        "kind": "loose_heading_rejected_on_divergence",
                        "title_key": sp["title_key"],
                        "template": matched,
                    }
                )
                metric_text = sp["metric"]
        # Определение EBITDA из договора применяется к ФИНАЛЬНОМУ тексту —
        # после всех подмен: оно главнее и извлечённой формулы (кейс боевого
        # прогона: модель взяла роллап при договорном «за вычетом Операционных
        # расходов»), и канона шаблонов (_EBITDA зашивает OTHER_OPEX, а договор
        # вправе выбрать второе прочтение). Сбой извлечения определения выше
        # по стеку — reading просто не приходит, поведение прежнее.
        trigger_text = sp["trigger"]
        if ebitda_reading:
            for label, current in (("metric", metric_text), ("trigger", trigger_text)):
                if not current:
                    continue
                rewritten = _apply_ebitda_reading(current, ebitda_reading)
                if rewritten is None:
                    continue
                if label == "metric":
                    metric_text = rewritten
                else:
                    trigger_text = rewritten
                match_alarms.append(
                    {
                        "kind": "ebitda_definition_applied",
                        "reading": ebitda_reading,
                        "target": label,
                        "text": rewritten,
                    }
                )
        cellspec = {
            "metric_ast": parse(metric_text),
            "metric_text": metric_text,
            "direction": sp["direction"],
            "limit": Decimal(sp["limit"]),
            "trigger_ast": parse(trigger_text) if trigger_text else None,
        }
        if match_alarms:
            cellspec["match_alarms"] = match_alarms
        if ebitda_needs_addback:
            # Проводка признака до rewrites.apply_final (задача 3): та же
            # схема, что у "direction" — поле cellspec, а не отдельный
            # аргумент через все промежуточные вызовы (_shadow_compare,
            # _cell_diagnostics читают его через .get(), как и direction).
            cellspec["ebitda_needs_addback"] = True
        # Тень для диагностики (не для расчёта): ячейку считает шаблон, но
        # извлечённая формула сохраняется, чтобы run_cell посчитал её вторым
        # проходом. Без этого подмена видна только текстами двух формул, а
        # единственный вопрос, который в окне имеет значение, — изменила ли
        # подмена ОТВЕТ — остаётся без ответа на 19 ячейках из 36.
        #
        # Условие — сам факт подмены, а НЕ diverged (ревью PR #21). diverged
        # слеп ровно там, где подмена меняет число молча: signature() намеренно
        # затирает знак Agg и константы, поэтому пара net/out с одинаковой
        # сигнатурой расхождением не считается — а знак прямо меняет actual.
        # Тем же слепым пятном накрыт весь класс матча по сигнатуре
        # (sp["template"]): у него сигнатуры равны по построению. Фильтровать
        # шум здесь нечем и незачем — у тени свой фильтр, алярм поднимается
        # только при changed_answer. Цена — лишний проход по леджеру там, где
        # формулы разошлись, а сигнатура совпала.
        if metric_text != sp["metric"]:
            cellspec["shadow_metric_text"] = sp["metric"]
        return cellspec, quote
    except (DslError, InvalidOperation, KeyError) as exc:
        return exc, quote


# --- harness -----------------------------------------------------------------


def skeleton(template_answers: dict) -> dict:
    """Каждая ячейка скелета — лестница без спеки: номер пункта известен уже
    из шаблона, и приор by_clause точнее глобального (75% против 64%)."""
    return {
        sc: {cl: fallback_cell(None, None, None, [], clause=cl)[0] for cl in cells}
        for sc, cells in template_answers.items()
    }


def dump_submission(sub: dict, template_answers: dict) -> None:
    """Атомарная запись submission. Инвариант «ключи == ключам шаблона»
    проверяется до записи: файл на диске никогда не расходится с шаблоном."""
    got = {(sc, cl) for sc, cells in sub["answers"].items() for cl in cells}
    want = {(sc, cl) for sc, cells in template_answers.items() for cl in cells}
    assert got == want, "ключи submission разошлись с шаблоном"
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = OUT / "submission.json.tmp"
    tmp.write_text(json.dumps(sub, ensure_ascii=False, indent=2))
    tmp.replace(OUT / "submission.json")


def _spec_only_fallback(
    scenario: str,
    clause: str,
    computed: list,
    exc: Exception,
    facts_source: str,
    specs_by_sc: dict[str, dict] | None,
    clause_map: dict[str, str],
    hide_templates: frozenset = frozenset(),
) -> tuple[dict, dict]:
    """Сценарий не загрузился: строк нет, но спека может быть известна —
    лестница сохраняет прочитанные направление и порог вместо скелета."""
    trace: dict = {"scenario": scenario, "clause": clause, "error": repr(exc)}
    try:
        cs: dict
        if facts_source == "extracted":
            sp = specs_by_sc[scenario]["clauses"].get(clause_map.get(clause, clause)) if specs_by_sc else None
            cs_or_error, _quote = _extracted_cellspec(sp, clause, scenario, hide_templates)
            if isinstance(cs_or_error, Exception):
                raise cs_or_error
            assert isinstance(cs_or_error, dict)
            cs = cs_or_error
        else:
            cs = legacy_spec_to_cellspec(_expected_specs()[scenario][clause])
        cell, alarms = fallback_cell(
            cs["direction"], family_of(cs["metric_ast"], cs["limit"]), cs["limit"], computed, clause=clause
        )
    except Exception as spec_exc:
        trace["spec_error"] = repr(spec_exc)
        # 5.7 и здесь (ревью PR #9, 9-я волна): направление/порог невалидной
        # спеки едут на ошибке (_extracted_cellspec) — как в run_cell.
        cell, alarms = fallback_cell(
            getattr(spec_exc, "spec_direction", None),
            None,
            getattr(spec_exc, "spec_limit", None),
            computed,
            clause=clause,
        )
    trace.update(path="prior", tier=2, alarms=alarms)
    return cell, trace


def _write_borrower_trace(
    wd: Path,
    scenario: str,
    rows: list,
    clauses,
    facts_source: str,
    specs_by_sc: dict | None = None,
    clause_map: dict[str, str] | None = None,
    index: dict | None = None,
) -> dict:
    """Пер-заёмщицкий трейс (раздел 6): категории всех строк и покрытие
    категоризации пишутся один раз на заёмщика, а не трижды по ячейкам.

    В extracted-режиме referenced-категории берутся из ИЗВЛЕЧЁННЫХ спек
    (ревью PR #9: на приватном наборе эталонных SPECS нет, и с ними
    referenced был бы всегда пуст — эскалация coverage_report до critical
    физически не срабатывала бы), документы — из досье. В expected-режиме
    документы пусты: факты пришли из эталона, не из PDF."""
    referenced: set[str] = set()
    for clause in sorted(clauses):
        try:
            if facts_source == "extracted":
                sp = (
                    specs_by_sc[scenario]["clauses"].get((clause_map or {}).get(clause, clause))
                    if specs_by_sc
                    else None
                )
                cs_or_error, _q = _extracted_cellspec(sp, clause, scenario)
                if not isinstance(cs_or_error, dict):
                    continue
                cs = cs_or_error
            else:
                cs = legacy_spec_to_cellspec(_expected_specs()[scenario][clause])
        except Exception:
            continue
        # Триггер наравне с метрикой: несработавший триггер даёт безусловный
        # COMPLIANT (evidence.compute), значит его категория — такой же путь к
        # статусу. referenced уходит в coverage_report и решает эскалацию
        # warn → critical; без триггера springing-ковенант, чьё условие читает
        # OTHER_OPEX или OPEX_TOTAL, оставил бы заёмщицкий алярм на warn.
        referenced |= _all_metric_categories(cs["metric_ast"])
        if cs.get("trigger_ast") is not None:
            referenced |= _all_metric_categories(cs["trigger_ast"])
    docs_used: list = []
    docs_rejected: list = []
    if facts_source == "extracted" and index is not None:
        try:
            acc = index["scenario_to_account"].get(scenario)
            dossier = json.loads((wd / "dossier" / f"{acc}.json").read_text())
            docs_used = sorted(d["file"] for d in dossier.get("docs", []))
            # С причиной, а не одним именем файла: отброс недействующей
            # редакции уносит из расчёта документальное решение, и в окне
            # прогона надо видеть не только ЧТО выпало, но и ПОЧЕМУ.
            docs_rejected = sorted(
                (
                    {"file": d.get("file", ""), "reason": d.get("reason", "")}
                    for d in dossier.get("docs_rejected", [])
                ),
                key=lambda d: (d["file"], d["reason"]),
            )
        except Exception:
            pass  # диагностика: досье могло не собраться — трейс не падает
    cov = coverage_report(rows, referenced)
    # Алярмы извлечённых спек — в borrower-трейс: trace/*.json сканируется
    # _alarm_counts, так пересчитанные при чтении алярмы (в артефакте specs
    # лежат только extraction-time) доезжают до run-report (10-я волна).
    spec_alarms = (
        (specs_by_sc or {}).get(scenario, {}).get("alarms", []) if facts_source == "extracted" else []
    )
    payload = {
        "scenario": scenario,
        "facts_source": facts_source,
        "docs_used": docs_used,
        "docs_rejected": docs_rejected,
        "categories": {r["txn_id"]: r["cat"] for r in sorted(rows, key=lambda x: x["txn_id"])},
        "coverage": cov,
        # Признак загрязнённости счёта — диагностический и сам по себе ничего
        # не значит (см. докстринг noise.py): наборы им неразделимы. В трейс он
        # идёт, чтобы в окне было видно, у каких заёмщиков широкое чтение
        # вообще опасно.
        "pollution_ratio": str(noise.pollution_ratio(rows)),
        "alarms": spec_alarms,
    }
    d = wd / "trace"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{scenario}.borrower.json").write_text(stable_json(payload))
    return cov


def _write_trace(wd: Path, scenario: str, clause: str, payload: dict) -> None:
    # Формат имени файла <scenario>.<clause>.json: делить по первой точке.
    # В сценариях точек нет; пункты (в них точка — часть номера) нужно
    # разбирать через split(".", 1), а не rsplit — иначе номер пункта потеряет
    # структуру.
    d = wd / "trace"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{scenario}.{clause}.json").write_text(stable_json(payload))


# --- run-report (репетиция, раздел 3: расхождение между прогонами — в отчёт) --


def _schema_versions() -> dict[str, int | str]:
    """Все *_VERSION-константы стадий — плоским списком `модуль.ИМЯ`."""
    out: dict[str, int | str] = {}
    for name in _VERSIONED_MODULES:
        mod = importlib.import_module(name)
        for attr in sorted(dir(mod)):
            if attr.endswith("_VERSION") and attr.isupper():
                out[f"{name}.{attr}"] = getattr(mod, attr)
    return out


def _alarm_kind(alarm) -> str:
    return str(alarm["kind"]) if isinstance(alarm, dict) and "kind" in alarm else "other"


def _alarm_counts(wd: Path) -> dict[str, int]:
    """Число алярмов по видам — собрано из тех же артефактов, что читает
    eval/invariants._collect_report_alarms (не импортируем её отсюда: там
    `import solve`, обратный импорт устроил бы цикл), плюс facts/specs.

    facts/specs обязательны отдельно от route/dossier: `facts_extraction_
    failed`/`specs_extraction_failed`/`no_agreement` запекаются ВНУТРЬ
    build()-результата stages.artifact (см. docs/ops/recovery-playbook.md) — деградация
    молча кэшируется под текущей версией стадии и без этого поля run-report
    выглядела бы чистой на отравленном work/<hash>."""
    alarms: list = []
    index_path = wd / "index.json"
    if index_path.exists():
        alarms += json.loads(index_path.read_text()).get("alarms", [])
    trace_dir = wd / "trace"
    seen_exact: set[str] = set()

    def add_once(alarm) -> None:
        # Один и тот же алярм-словарь живёт в нескольких местах (спековые —
        # в borrower-трейсе И в артефакте specs; карантинные — во всех
        # пер-аккаунтных досье): точный дубль считается один раз
        # (ревью PR #9, 11-я и 14-я волны).
        key = stable_json(alarm)
        if key in seen_exact:
            return
        seen_exact.add(key)
        alarms.append(alarm)

    if trace_dir.is_dir():
        seen_fx: set[tuple[str, str]] = set()
        for p in sorted(trace_dir.glob("*.json")):
            payload = json.loads(p.read_text())
            for a in payload.get("alarms", []):
                add_once(a)
            # fx-алярм — уровня заёмщика, но лежит в каждом из его ячейковых
            # трейсов: без дедупа run-report считал бы его трижды (ревью PR #9).
            scenario = p.stem.split(".", 1)[0]
            for fx in payload.get("fx_alarms", []):
                key = (scenario, stable_json(fx))
                if key not in seen_fx:
                    seen_fx.add(key)
                    alarms.append(fx)
    for sub_dir in ("route", "dossier", "facts", "specs"):
        d = wd / sub_dir
        if d.is_dir():
            for p in sorted(d.glob("*.json")):
                for a in json.loads(p.read_text()).get("alarms", []):
                    add_once(a)
    counts: dict[str, int] = {}
    for a in alarms:
        kind = _alarm_kind(a)
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _tier_breakdown(wd: Path) -> dict[str, int]:
    """Ячейки по ярусам лестницы (0 dsl / 1 heuristic / 2 prior) — из уже
    записанных трейсов, без повторного прохода по answers."""
    counts: dict[str, int] = {}
    trace_dir = wd / "trace"
    if not trace_dir.is_dir():
        return counts
    for p in sorted(trace_dir.glob("*.json")):
        if p.stem.endswith(".borrower"):
            continue
        tier = json.loads(p.read_text()).get("tier")
        key = str(tier) if tier is not None else "none"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _git_sha() -> str | None:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5, check=True
        )
        return res.stdout.strip()
    except Exception:
        return None  # вне git-репозитория/бинарь недоступен — не критично для отчёта


def _build_run_report(archive: Path, ds_hash: str, wd: Path, duration_s: float) -> dict:
    model = llm.GEMINI_MODEL if llm._provider() == "gemini" else llm.MODEL
    return {
        "dataset_hash": ds_hash,
        "archive_sha256": hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
        "model": model,
        "schema_versions": _schema_versions(),
        "budget": llm.budget_state(),
        "tier_breakdown": _tier_breakdown(wd),
        "alarm_counts": _alarm_counts(wd),
        "git_sha": _git_sha(),
        "duration_s": duration_s,
        # Вердикт «прогон по публичному набору» пишется здесь, где архив под
        # рукой, а не выводится потребителем из хранимого отпечатка (ревью
        # PR #18, круг 5). eval/public_baseline.json константой не является:
        # `sanity.py <любой>.zip --write-baseline` кладёт туда dataset_hash
        # переданного архива, и ре-baseline по приватному набору (прямая
        # рекомендация docs/ops/fresh-workdir-noise-diagnosis.md) сделал бы
        # «публичным отпечатком» приватный хеш. Сравнение байтов леджера от
        # этого не зависит вовсе, а его собственный fail-safe — «любой сбой
        # сопоставления — не публичный».
        "is_public_dataset": _is_public_dataset(archive, ROOT / "dataset" / "agentic-bank-public"),
    }


def _cell_diagnostics(
    trace: dict,
    rows: list,
    cellspec: dict,
    quote: str,
    account: str,
    pollution: Decimal,
    scenario: str,
    clause: str,
    doc_facts: dict | None = None,
    doc_fact_quotes: dict | None = None,
) -> None:
    """Три диагностики поверх УЖЕ посчитанной ячейки: знак, шум, неразнесённые.

    Все три смотрят на ПОСЛЕ-переписанную метрику. run_cell прогоняет спеку
    через rewrites.apply_final (сужение опекса, квартализация) и считает уже
    её, а cellspec на входе остаётся прежним — apply_final не мутирует.
    Диагностика по исходному AST говорит про категорию, которую ячейка не
    читает: ревью финальной ветки нашло ровно это у sign_divergence и
    other_unassigned на пяти ячейках приватного прогона (сторно доложено по
    роллапу, тогда как метрика считала статью). apply_final чистая и
    идемпотентная, поэтому зовётся здесь один раз на все три.

    Ни одна из трёх не имеет права стоить ячейки — ответ уже собран, — поэтому
    каждая под своим except, а сбой самого переписывания оставляет исходную
    метрику: врущая диагностика дешевле отсутствующей.
    """
    try:
        final_spec, _rw = rewrites.apply_final(
            cellspec,
            quote,
            cellspec.get("direction"),
            cellspec.get("ebitda_needs_addback", False),
            doc_facts=doc_facts,
            doc_fact_quotes=doc_fact_quotes,
        )
    except Exception as exc:
        trace["diagnostics_rewrite_error"] = repr(exc)
        final_spec = cellspec
    metric_ast = final_spec["metric_ast"]
    trigger_ast = final_spec.get("trigger_ast")

    # Знак расходной категории: дефолт out, а расхождение с net значит сторно
    # внутри читаемой категории. Категории могут прийти от LLM — падение
    # диагностики (KeyError в expand) не должно отбросить уже посчитанную
    # ячейку.
    try:
        divergence = sign_divergence(rows, _metric_categories(metric_ast))
        if divergence:
            trace["sign_divergence"] = divergence
    except Exception as exc:
        trace["sign_divergence_error"] = repr(exc)

    # Шум леджера дошёл до расчёта: счёт загрязнён И метрика читает роллап, не
    # суженный перечнем. По отдельности ни одно из двух ничего не значит,
    # поэтому алярм только на сочетании. Диагностика: строки не отбрасываются,
    # вердикт не меняется, падение обхода не стоит ячейки.
    try:
        na = noise.rollup_alarm(account, pollution, metric_ast)
        if na is not None:
            # scenario/clause внутрь словаря по той же причине, что у
            # other_unassigned: точный дедуп в _alarm_counts схлопнул бы разные
            # ячейки в одну.
            trace.setdefault("alarms", []).append({**na, "scenario": scenario, "clause": clause})
            print(
                f"ALARM polluted_rollup_read {scenario} {clause}: "
                f"categories={','.join(na['categories'])} ratio={na['ratio']}",
                flush=True,
            )
    except Exception as exc:
        trace["polluted_rollup_read_error"] = repr(exc)

    # Неразнесённые строки глазами этой ячейки (5.3): диагностика, вердикт не
    # меняется. Падение обхода не стоит ячейки.
    #
    # Категории триггера учитываются наравне с категориями метрики:
    # несработавший триггер даёт COMPLIANT безусловно (evidence.compute),
    # поэтому потерянная строка в категории, которую читает только триггер,
    # молча переворачивает статус так же, как строка в метрике.
    try:
        alarm_categories = _all_metric_categories(metric_ast)
        if trigger_ast is not None:
            alarm_categories |= _all_metric_categories(trigger_ast)
        oa = cell_other_alarm(rows, alarm_categories, _metric_filters(metric_ast, trigger_ast))
        if oa is not None:
            trace["other_unassigned"] = oa
            # И в общий alarms: сканеры run-report (_alarm_counts) и
            # invariants._collect_report_alarms читают только alarms/fx_alarms,
            # а строка ALARM в stdout тонет между fx и fallback. В окне решают
            # по run-report — тот же приём, что для metric_substituted.
            # scenario/clause внутрь словаря: иначе точный дедуп схлопнул бы
            # одинаковые срабатывания разных ячеек в одно.
            trace.setdefault("alarms", []).append(
                {
                    "kind": "other_unassigned",
                    "scenario": scenario,
                    "clause": clause,
                    "blind": oa["blind"],
                    "severity": oa["severity"],
                    # severity=None (inputs_sum == 0) — это MAX, а не
                    # отсутствие тяжести. В stdout это уже учтено, но решают по
                    # run-report, и там сортировка по null уронила бы такую
                    # ячейку вниз или упала бы с TypeError. Флаг даёт
                    # сортируемый ключ: (not inputs_empty, severity).
                    "inputs_empty": oa["severity"] is None,
                    "other_sum": oa["other_sum"],
                }
            )
            # severity=None означает inputs_sum == 0: метрика не видит НИ ОДНОЙ
            # своей строки, весь объём осел в OTHER. Это максимальная тяжесть,
            # и печатать её как None нельзя — разбор в окне идёт сортировкой по
            # severity, и такая ячейка встала бы ниже любой с посчитанной долей.
            print(
                f"ALARM other_unassigned {scenario} {clause}: "
                f"blind={','.join(oa['blind'])} "
                f"severity={'MAX(inputs=0)' if oa['severity'] is None else oa['severity']} "
                f"other_sum={oa['other_sum']}",
                flush=True,
            )
    except Exception as exc:
        trace["other_unassigned_error"] = repr(exc)


def main(
    archive: Path, facts_source: str = "extracted", hide_templates: frozenset[str] = frozenset()
) -> dict:
    """hide_templates (LOBO, 7.3) — сценарии, для которых библиотека шаблонов
    отключена: ячейка считается по сырому DSL спеки вместо TEMPLATES."""
    assert facts_source in ("expected", "extracted"), f"неизвестный источник фактов {facts_source!r}"
    start = time.monotonic()
    archive = Path(archive)
    ds_hash, input_dir = extract_archive(archive)
    print(f"dataset_hash: {ds_hash}", flush=True)
    # Каким провайдером/моделью реально идёт прогон — рядом с dataset_hash:
    # LLM_PROVIDER молча переключает бэкенд (llm._provider(), дефолт anthropic),
    # и без явного лога это не видно ни в логе прогона, ни в кассете (ревью PR #12,
    # круг 4).
    active_model = llm.GEMINI_MODEL if llm._provider() == "gemini" else llm.MODEL
    print(f"provider: {llm._provider()} {active_model}", flush=True)
    wd = workdir(ds_hash)

    # Скелет — как можно раньше: без шаблона нельзя построить даже его, всё
    # остальное (леджер, индекс, расчёт) уже падает поверх валидного файла.
    try:
        inputs = find_inputs(input_dir)
    except Exception as exc:
        # Последний рубеж перед скелетом (ревью перед окном). find_inputs зовётся
        # раньше первой записи, и любое её исключение под `set -e` в run.sh
        # оставляло бы out/ с файлом ПРОШЛОГО прогона — на репетициях публичного.
        # Неоднозначность CSV здесь уже не падает (ledger._pick_ledger), но
        # остаются другие входы: ноль шаблонов, два шаблона, битая распаковка.
        # Свой пустой скелет честнее чужих готовых ответов, поэтому пробуем
        # записать его по любому найденному шаблону и только потом падаем.
        print(f"ALARM find_inputs_failed: {exc!r}", flush=True)
        found = sorted(input_dir.rglob("submission_template.json"))
        if found:
            try:
                tpl = json.loads(found[0].read_text())
                dump_submission({**submission_meta(), "answers": skeleton(tpl["answers"])}, tpl["answers"])
                print(f"ALARM skeleton_written_after_failure: {found[0].name}", flush=True)
            except Exception as inner:
                print(f"ALARM skeleton_write_failed: {inner!r}", flush=True)
        raise
    template = json.loads(inputs["template"].read_text())
    answers: dict = skeleton(template["answers"])
    sub = {**submission_meta(), "answers": answers}  # answers — та же ссылка, правки видны в sub
    dump_submission(sub, template["answers"])
    # Отчёт прошлого прогона снимается вместе с записью скелета (ревью PR #18,
    # круг 4). Он пишется ПОСЛЕДНИМ, а скелет — первым, поэтому прерванный
    # прогон иначе оставлял бы пару «свежий submission + отчёт прошлого
    # прогона», и submit.py судил бы о происхождении по чужому прогону: хеш,
    # оставшийся от репетиции на публичном архиве, обернулся бы отказом снять
    # снапшот с приватных ответов упавшего боевого прогона. Отсутствие отчёта
    # означает «происхождение не установлено», а это fail-open — ровно то, чего
    # требует точка принятия решений ранбука про упавший прогон.
    #
    # Обёрнуто тем же приёмом, что и запись отчёта в конце main (ревью PR #18,
    # круг 8): снятие решает ту же диагностическую задачу и ячейки стоить не
    # может, а `missing_ok=True` глушит только FileNotFoundError — каталог с
    # этим именем или неудачные права на out/ уронили бы весь прогон здесь, до
    # первого посчитанного сценария.
    try:
        (OUT / "run-report.json").unlink(missing_ok=True)
    except Exception as exc:
        print(f"ALARM stale_run_report_unlink_failed: {exc!r}", flush=True)

    targets = sorted(template["answers"])
    ledger_art = load_ledger(wd, input_dir, target_scenarios=targets)
    all_rows = rows_of(ledger_art)
    index = artifact(wd / "index.json", INDEX_VERSION, lambda: build_index(all_rows, targets))
    # Строки без разобранной суммы отбираются наравне с прочими: расчёт их
    # отбросит, если факты досье не вернут им сумму. В индекс они не идут —
    # счёт сценария определяется по строкам, которые действительно посчитаны.
    scenario_rows = all_rows + dirty_rows_of(ledger_art)
    # Алярмы леджера (отвергнутые категории, грязные суммы) — в лог прогона:
    # на приватном наборе иначе они останутся невидимыми в трёхчасовом окне.
    for alarm in ledger_art.get("alarms", []):
        print(f"ALARM {alarm}", flush=True)
    for alarm in index["alarms"]:
        print(f"ALARM {alarm}", flush=True)

    # Документный конвейер — до цикла по заёмщикам: параллелизм LLM-вызовов
    # живёт внутри него (build_dossiers, задача 21), сам цикл по ячейкам
    # ниже — последовательный и детерминированный.
    facts_by_sc: dict[str, dict] = {}
    specs_by_sc: dict[str, dict] = {}
    if facts_source == "extracted":
        try:
            facts_by_sc, specs_by_sc = _extracted_inputs(wd, input_dir, index, targets)
        except Exception as exc:  # fail-open последней инстанции: документный конвейер целиком
            print(f"ALARM extracted_inputs_failed: {exc!r}", flush=True)
            facts_by_sc = {sc: _with_doc_facts(facts_extract._empty_facts()) for sc in targets}
            specs_by_sc = {
                sc: {"clauses": {}, "alarms": [{"kind": "extracted_inputs_failed", "error": repr(exc)}]}
                for sc in targets
            }

    def _facts_for(scenario: str) -> dict:
        return facts_by_sc[scenario] if facts_source == "extracted" else _facts_of(scenario)

    # Диагностика 5.6: общее число непустых улик и доля на коэффициентных
    # метриках — резкий рост второй цифры значит, что D собрано слишком широко.
    emitted = ratio_emitted = 0
    # Посчитанные ярусом dsl пары (направление, actual) — медианная ступень
    # лестницы для ячеек без прочитанного порога.
    computed: list[tuple[str, float]] = []
    for scenario in targets:
        clause_map: dict[str, str] = {}
        if facts_source == "extracted":
            clause_map, unmatched = _match_clauses(
                sorted(template["answers"][scenario]), sorted(specs_by_sc[scenario]["clauses"])
            )
            for clause in unmatched:
                print(f"ALARM clause_unmatched {scenario} {clause}", flush=True)
            for t_cl, e_cl in sorted(clause_map.items()):
                if t_cl != e_cl:
                    # Суффикс-доматч — не тишина (ревью PR #9, 4-я волна):
                    # подмена номера пункта видима оператору и в трейсе ячейки.
                    print(f"ALARM clause_remapped {scenario}: {t_cl} -> {e_cl}", flush=True)
            # Алярмы спек (invalid_spec/missing_doc_keys/limit_outlier/...) —
            # в stdout: без этого они умирали в specs_by_sc непрочитанными
            # (ревью PR #9, 10-я волна), а limit_outlier вообще не имел эффекта.
            for alarm in specs_by_sc[scenario].get("alarms", []):
                print(f"ALARM spec_{alarm.get('kind', 'other')} {scenario}: {alarm}", flush=True)
        try:
            # Внутри try: даже единственная точка чтения фактов не имеет права
            # уронить цикл — сценарий уйдёт лестницей, остальные посчитаются.
            facts = _facts_for(scenario)
            raw, rows, fx_alarms = load_rows(
                scenario, scenario_rows, index, facts, _donor_rates(targets, scenario, _facts_for)
            )
        except Exception as exc:  # fail-open: ячейки приходят лестницей без строк
            print(f"ALARM scenario_failed {scenario}: {exc!r}", flush=True)
            for clause in sorted(template["answers"][scenario]):
                cell, trace = _spec_only_fallback(
                    scenario, clause, computed, exc, facts_source, specs_by_sc, clause_map, hide_templates
                )
                answers[scenario][clause] = cell
                try:
                    dump_submission(sub, template["answers"])
                    _write_trace(wd, scenario, clause, trace)
                except Exception as wexc:
                    print(f"ALARM trace_write_failed {scenario} {clause}: {wexc!r}", flush=True)
            continue
        for alarm in fx_alarms:
            print(f"ALARM {alarm}", flush=True)
        # Один раз на заёмщика: признак — чистая функция от его строк, а
        # читают его все ячейки. Под try по общему правилу: диагностика не
        # имеет права стоить ячейки, а ноль означает «признак не посчитан» и
        # просто оставляет алярм молчать.
        try:
            pollution = noise.pollution_ratio(rows)
        except Exception as exc:
            print(f"ALARM pollution_ratio_failed {scenario}: {exc!r}", flush=True)
            pollution = Decimal(0)
        # Диагностика — не расчёт: её падение не должно стоить ни одной ячейки.
        try:
            cov = _write_borrower_trace(
                wd,
                scenario,
                rows,
                template["answers"][scenario],
                facts_source,
                specs_by_sc,
                clause_map,
                index,
            )
            if cov["alarm"] != "none":
                share = f"{cov['other_share']:.4f}"
                print(f"ALARM category_coverage {scenario}: {cov['alarm']} other_share={share}", flush=True)
        except Exception as exc:
            print(f"ALARM borrower_trace_failed {scenario}: {exc!r}", flush=True)
        for clause in sorted(template["answers"][scenario]):
            trace = {"scenario": scenario, "clause": clause}
            cell = answers[scenario][clause]  # скелет — фолбэк последней инстанции
            try:
                quote = ""
                try:
                    if facts_source == "extracted":
                        sp = specs_by_sc[scenario]["clauses"].get(clause_map.get(clause, clause))
                        sc_ebitda_def = specs_by_sc[scenario].get("ebitda_reading") or {}
                        cellspec_or_error, quote = _extracted_cellspec(
                            sp,
                            clause,
                            scenario,
                            hide_templates,
                            fact_keys=frozenset(facts.get("doc_facts", {})),
                            ebitda_reading=sc_ebitda_def.get("reading"),
                            ebitda_needs_addback=sc_ebitda_def.get("needs_addback", False),
                        )
                    else:
                        cellspec_or_error = legacy_spec_to_cellspec(_expected_specs()[scenario][clause])
                except Exception as exc:
                    cellspec_or_error = exc
                cell, trace = run_cell(scenario, clause, raw, facts, cellspec_or_error, computed, quote=quote)
                if clause_map.get(clause, clause) != clause:
                    trace["extracted_clause"] = clause_map[clause]  # суффикс-доматч виден в трейсе
                if trace.get("tier") != 0:
                    print(
                        f"ALARM cell_fallback {scenario} {clause}: tier={trace.get('tier')}",
                        flush=True,
                    )
                if fx_alarms:
                    trace["fx_alarms"] = fx_alarms
                if isinstance(cellspec_or_error, dict):
                    _cell_diagnostics(
                        trace,
                        rows,
                        cellspec_or_error,
                        quote,
                        index["scenario_to_account"].get(scenario, ""),
                        pollution,
                        scenario,
                        clause,
                        doc_facts=facts.get("doc_facts"),
                        doc_fact_quotes=facts.get("doc_fact_quotes"),
                    )
                    if trace.get("tier") == 0:
                        computed.append((cellspec_or_error["direction"], cell["actual"]))
                    if cell["evidence_txn_id"] is not None:
                        emitted += 1
                        if isinstance(cellspec_or_error["metric_ast"], Ratio):
                            ratio_emitted += 1
            except Exception as exc:  # fail-open последней инстанции: ячейка — скелет
                trace["error"] = repr(exc)
                print(f"ALARM cell_failed {scenario} {clause}: {exc!r}", flush=True)
            answers[scenario][clause] = cell
            try:
                dump_submission(sub, template["answers"])
            except Exception as exc:
                print(f"ALARM submission_write_failed {scenario} {clause}: {exc!r}", flush=True)
            try:
                _write_trace(wd, scenario, clause, trace)
            except Exception as exc:
                print(f"ALARM trace_write_failed {scenario} {clause}: {exc!r}", flush=True)
    print(f"evidence emitted: {emitted}, of them on ratio-metrics: {ratio_emitted}", flush=True)
    # Отчёт о прогоне — диагностика после того, как все ячейки уже записаны:
    # его падение не может стоить ни одной ячейки, только самого отчёта.
    try:
        report = _build_run_report(archive, ds_hash, wd, time.monotonic() - start)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "run-report.json").write_text(stable_json(report))
    except Exception as exc:
        print(f"ALARM run_report_failed: {exc!r}", flush=True)
    return answers


def _is_public_dataset(archive: Path, public_dir: Path) -> bool:
    """Прогон идёт по тому набору, к которому относится публичный ключ?

    ground_truth.json лежит в репозитории всегда, поэтому на приватном архиве
    скорер сравнил бы приватные ответы с публичной разметкой и напечатал бы
    «ИТОГО: 0.xx / 36.00» с частоколом `<<<`. Под таймером 9 августа это
    читается как катастрофа и провоцирует отладку на ровном месте. Сравнение —
    побайтово по леджеру; имена файлов не зашиты, оба находятся тем же
    find_inputs, что и основной поток. Любой сбой сопоставления — «не
    публичный»: молчание безопаснее ложной тревоги.
    """
    try:
        _, input_dir = extract_archive(archive)
        run_csv = find_inputs(input_dir)["ledger_csv"]
        public_csv = find_inputs(public_dir)["ledger_csv"]
    except Exception:
        return False
    return run_csv.read_bytes() == public_csv.read_bytes()


if __name__ == "__main__":
    from score import score as _score

    _archive = Path(sys.argv[1])
    answers = main(_archive)
    public_dir = ROOT / "dataset" / "agentic-bank-public"
    gt_path = public_dir / "ground_truth.json"
    if gt_path.exists() and _is_public_dataset(_archive, public_dir):
        _score(answers, json.loads(gt_path.read_text())["scenarios"])
    else:
        print("скорер пропущен: публичный ground_truth не от этого набора", flush=True)
