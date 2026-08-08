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

import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, "solution")
sys.path.insert(0, "eval")

from expected_extraction import FACTS, SPECS

import evidence
import facts_extract
import llm
from dossier import build_dossiers
from dsl import Agg, Doc, DslError, Ratio, parse, signature, walk
from engine import agg, prepare_rows, select_rows, sign_divergence
from facts_extract import extract_facts, resolve_doc_fact
from fallbacks import fallback_cell, family_of, heuristic_template
from fx import coverage_alarms, to_usd
from ledger import dirty_rows_of, extract_archive, find_inputs, load_ledger, rows_of
from scindex import INDEX_VERSION, build_index
from specs_extract import extract_specs
from stages import artifact
from taxonomy import coverage_report
from templates import TEMPLATES, match_heading
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


# Ключи doc_facts, которые вычисляет код из сырых фактов досье (_with_doc_facts):
# адресный резолв (resolve_doc_fact) их не трогает — LLM не делает арифметику.
_DERIVED_DOC_KEYS = frozenset({"ebitda_addbacks_material_total"})


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
    if model_value is not None and str(model_value) != derived_total:
        print(
            f"ALARM derived_doc_key_overridden: ebitda_addbacks_material_total "
            f"модели ({model_value}) заменён арифметикой кода ({derived_total})",
            flush=True,
        )
    doc_facts["ebitda_addbacks_material_total"] = derived_total
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
    if scenario not in FACTS:
        print(f"ALARM facts_missing {scenario}: расчёт без фактов досье", flush=True)
    return _with_doc_facts(FACTS.get(scenario, {}))


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


def _metric_inputs(node, raw: list, facts: dict) -> dict:
    """Входы формулы для трейса: агрегат каждой пары (категория, знак) из AST."""
    rows = prepare_rows(raw, facts)
    return {
        f"{n.category}:{n.sign}": str(agg(rows, n.category, n.sign)) for n in walk(node) if isinstance(n, Agg)
    }


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
        cellspec = cellspec_or_error
        trace["spec"] = {
            "quote": quote,
            "direction": cellspec["direction"],
            "limit": str(cellspec["limit"]),
            "metric": cellspec.get("metric_text", ""),
        }
        for alarm in cellspec.get("match_alarms", []):
            trace.setdefault("match_alarms", []).append(alarm)
            print(f"ALARM {alarm['kind']} {scenario} {clause}: {alarm}")
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
            trace.update(path="prior", tier=2, alarms=alarms)
            return cell, trace
    trace["spec_error"] = repr(cellspec_or_error)
    # 5.7: прочитанные направление и порог невалидной спеки не выбрасываются —
    # _extracted_cellspec вешает их на ошибку, лестница доносит до приора
    # (ревью PR #9, 6-я волна).
    spec_direction = getattr(cellspec_or_error, "spec_direction", None)
    spec_limit = getattr(cellspec_or_error, "spec_limit", None)
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
            cell, alarms = fallback_cell(
                spec_direction, family_of(metric_ast, spec_limit), spec_limit, computed, clause=clause
            )
            cell["actual"] = q2(abs(res.value))
            trace.update(path="heuristic_template", tier=1, template=tpl, alarms=alarms)
            return cell, trace
        except Exception as exc:
            trace["heuristic_error"] = repr(exc)
    cell, alarms = fallback_cell(spec_direction, None, spec_limit, computed, clause=clause)
    trace.update(path="prior", tier=2, alarms=alarms)
    return cell, trace


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
                    resolved = resolve_doc_fact(wd, dossiers[acc], key, sp["quote"])
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
        except Exception as exc:
            print(f"ALARM specs_failed {sc}: {exc!r}", flush=True)
            spec_art = {
                "clauses": {},
                "alarms": [{"kind": "specs_failed", "scenario": sc, "error": repr(exc)}],
            }
        facts_by_sc[sc] = _with_doc_facts(facts)
        specs_by_sc[sc] = spec_art
    return facts_by_sc, specs_by_sc


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


def _extracted_cellspec(
    sp: dict | None,
    clause: str,
    scenario: str = "",
    hide_templates: frozenset = frozenset(),
    fact_keys: frozenset | None = None,
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
        template = match_heading(sp["title_key"]) or sp["template"]
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
                metric_text = sp["metric"]
        cellspec = {
            "metric_ast": parse(metric_text),
            "metric_text": metric_text,
            "direction": sp["direction"],
            "limit": Decimal(sp["limit"]),
            "trigger_ast": parse(sp["trigger"]) if sp["trigger"] else None,
        }
        # Подмена извлечённого DSL шаблоном остаётся (ruling: шаблон при матче
        # исполняется), но обязана быть видимой ЦЕЛИКОМ (ревью PR #9, 10-я
        # волна): не только другой набор категорий, но и разница по
        # quarter()/period()/знаку/форме агрегации — сигнатурное сравнение.
        if metric_text != sp["metric"]:
            div = _category_divergence(sp["metric"], metric_text)
            if div:
                cellspec["match_alarms"] = [
                    {"kind": "heading_category_divergence", "extracted": div[0], "template": div[1]}
                ]
            else:
                try:
                    if signature(parse(sp["metric"])) != signature(parse(metric_text)):
                        cellspec["match_alarms"] = [
                            {
                                "kind": "heading_signature_divergence",
                                "extracted": sp["metric"],
                                "template": metric_text,
                            }
                        ]
                except DslError:
                    pass  # сырой DSL не парсится — подмена и так единственный путь
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
            cs = legacy_spec_to_cellspec(SPECS[scenario][clause])
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
                cs = legacy_spec_to_cellspec(SPECS[scenario][clause])
        except Exception:
            continue
        referenced |= {n.category for n in walk(cs["metric_ast"]) if isinstance(n, Agg)}
    docs_used: list = []
    docs_rejected: list = []
    if facts_source == "extracted" and index is not None:
        try:
            acc = index["scenario_to_account"].get(scenario)
            dossier = json.loads((wd / "dossier" / f"{acc}.json").read_text())
            docs_used = sorted(d["file"] for d in dossier.get("docs", []))
            docs_rejected = sorted(d.get("file", "") for d in dossier.get("docs_rejected", []))
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
    build()-результата stages.artifact (см. recovery-playbook.md) — деградация
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
    }


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
    wd = workdir(ds_hash)

    # Скелет — как можно раньше: без шаблона нельзя построить даже его, всё
    # остальное (леджер, индекс, расчёт) уже падает поверх валидного файла.
    inputs = find_inputs(input_dir)
    template = json.loads(inputs["template"].read_text())
    answers: dict = skeleton(template["answers"])
    sub = {**submission_meta(), "answers": answers}  # answers — та же ссылка, правки видны в sub
    dump_submission(sub, template["answers"])

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
                        cellspec_or_error, quote = _extracted_cellspec(
                            sp,
                            clause,
                            scenario,
                            hide_templates,
                            fact_keys=frozenset(facts.get("doc_facts", {})),
                        )
                    else:
                        cellspec_or_error = legacy_spec_to_cellspec(SPECS[scenario][clause])
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
                    # Знак расходной категории: дефолт out, а расхождение с net
                    # значит сторно внутри читаемой категории. Категории могут
                    # прийти от LLM — падение диагностики (KeyError в expand)
                    # не должно отбросить уже посчитанную ячейку.
                    try:
                        divergence = sign_divergence(
                            rows, _metric_categories(cellspec_or_error["metric_ast"])
                        )
                        if divergence:
                            trace["sign_divergence"] = divergence
                    except Exception as exc:
                        trace["sign_divergence_error"] = repr(exc)
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


if __name__ == "__main__":
    from score import score as _score

    answers = main(Path(sys.argv[1]))
    gt_path = Path("dataset/agentic-bank-public/ground_truth.json")
    if gt_path.exists():
        _score(answers, json.loads(gt_path.read_text())["scenarios"])
