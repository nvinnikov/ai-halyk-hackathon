"""Harness: скелет-первым submission, fail-open на ячейку, трейс.

Submission пишется задом наперёд (раздел 6): сначала на диск кладётся
полностью заполненный фолбэками скелет, каждая посчитанная ячейка
перезаписывает свою — на любой секунде прогона на диске валидный файл.
Скелет строится сразу после распаковки и чтения шаблона, до леджера и
индекса: всё, что может упасть после этого, оставляет на диске валидный
файл вместо пустоты.

Вычислительное ядро — лестница run_cell (5.7): спека в DSL → эвристика по
цитате пункта → приор; null в actual не существует как состояние. Спеки и
факты пока эталонные (мост legacy_spec_to_cellspec), задача 24 подменит их
на извлечённые, не трогая harness.
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "solution")
sys.path.insert(0, "eval")

from expected_extraction import FACTS, SPECS

import evidence
from dsl import Agg, Ratio, parse, walk
from engine import agg, prepare_rows, select_rows, sign_divergence
from fallbacks import fallback_cell, family_of, heuristic_template
from fx import coverage_alarms, to_usd
from ledger import dirty_rows_of, extract_archive, find_inputs, load_ledger, rows_of
from scindex import INDEX_VERSION, build_index
from stages import artifact
from taxonomy import coverage_report
from templates import TEMPLATES
from util import OUT, q2, stable_json, workdir

SUBMISSION_META = {"team": "", "contact_email": "", "model": ""}


# --- ядро на эталонных фактах (источник подменяется задачами 16/24) ----------


def _with_doc_facts(facts: dict) -> dict:
    """doc_facts для doc()-метрик DSL: считается детерминированно из сырых
    фактов досье — арифметика (порог материальности, сумма добавок) остаётся
    в коде, LLM отдаёт только исходные числа."""
    out = dict(facts)
    doc_facts = dict(out.get("doc_facts", {}))
    addbacks = [Decimal(str(a)) for a in out.get("ebitda_addbacks", [])]
    materiality = Decimal(str(out.get("addback_materiality", 0)))
    doc_facts.setdefault(
        "ebitda_addbacks_material_total",
        str(sum((a for a in addbacks if a >= materiality), Decimal(0))),
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
    tpl = heuristic_template(quote)
    if tpl is not None:
        try:
            metric_ast = parse(TEMPLATES[tpl])
            # Порога нет — эвристика даёт только метрику; статус возьмёт приор.
            _, res = evidence.compute(
                raw,
                facts,
                {"metric_ast": metric_ast, "direction": "max", "limit": Decimal(0), "trigger_ast": None},
            )
            cell, alarms = fallback_cell(None, family_of(metric_ast, None), None, computed, clause=clause)
            cell["actual"] = q2(abs(res.value))
            trace.update(path="heuristic_template", tier=1, template=tpl, alarms=alarms)
            return cell, trace
        except Exception as exc:
            trace["heuristic_error"] = repr(exc)
    cell, alarms = fallback_cell(None, None, None, computed, clause=clause)
    trace.update(path="prior", tier=2, alarms=alarms)
    return cell, trace


def _donor_rates(targets: list[str], scenario: str) -> list[dict]:
    """Донорская ступень лестницы: курсы всех прочих целевых заёмщиков.

    Порядок доноров фиксирован, чтобы выбор не зависел от порядка сценариев
    в шаблоне; окончательный тай-брейк — в fx.pick_rate."""
    return sorted(
        (r for other in targets if other != scenario for r in _facts_of(other).get("fx_rates", [])),
        key=lambda r: (r.get("doc_date") or "", r.get("doc_hash") or "", str(r.get("usd_per_unit"))),
    )


def scenario_inputs(archive: Path, scenario: str) -> tuple[list[dict], dict]:
    """Строки заёмщика после fx-конвертации + факты (пока эталонные), факты
    уже пропущены через _with_doc_facts. Переиспользуется тестами и main-путём
    через те же стадии; повторный вызов дёшев — стадии кэшируются в work/<hash>.

    В отличие от main, скелет submission здесь не строится: это read-only
    вход для парити-теста и контрфактуалов задачи 16."""
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
    facts = _facts_of(scenario)
    raw, _rows, _alarms = load_rows(scenario, scenario_rows, index, facts, _donor_rates(targets, scenario))
    return raw, facts


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


def _spec_only_fallback(scenario: str, clause: str, computed: list, exc: Exception) -> tuple[dict, dict]:
    """Сценарий не загрузился: строк нет, но спека известна — лестница
    сохраняет прочитанные направление и порог вместо скелета."""
    trace: dict = {"scenario": scenario, "clause": clause, "error": repr(exc)}
    try:
        cs = legacy_spec_to_cellspec(SPECS[scenario][clause])
        cell, alarms = fallback_cell(
            cs["direction"], family_of(cs["metric_ast"], cs["limit"]), cs["limit"], computed, clause=clause
        )
    except Exception as spec_exc:
        trace["spec_error"] = repr(spec_exc)
        cell, alarms = fallback_cell(None, None, None, computed, clause=clause)
    trace.update(path="prior", tier=2, alarms=alarms)
    return cell, trace


def _write_borrower_trace(wd: Path, scenario: str, rows: list, clauses, facts_source: str) -> dict:
    """Пер-заёмщицкий трейс (раздел 6): категории всех строк и покрытие
    категоризации пишутся один раз на заёмщика, а не трижды по ячейкам.
    Документы в expected-режиме пусты: факты пришли из эталона, не из PDF."""
    referenced: set[str] = set()
    for clause in sorted(clauses):
        try:
            cs = legacy_spec_to_cellspec(SPECS[scenario][clause])
        except Exception:
            continue
        referenced |= {n.category for n in walk(cs["metric_ast"]) if isinstance(n, Agg)}
    cov = coverage_report(rows, referenced)
    payload = {
        "scenario": scenario,
        "facts_source": facts_source,
        "docs_used": [],
        "docs_rejected": [],
        "categories": {r["txn_id"]: r["cat"] for r in sorted(rows, key=lambda x: x["txn_id"])},
        "coverage": cov,
    }
    d = wd / "trace"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{scenario}.borrower.json").write_text(stable_json(payload))
    return cov


def _write_trace(wd: Path, scenario: str, clause: str, payload: dict) -> None:
    # Имя файла «<сценарий>.<пункт>.json» потребители разбирают как
    # stem.split(".", 1): сценарий точек не содержит, пункт («6.1») содержит.
    # rsplit здесь неверен и молча даст пункт «1».
    d = wd / "trace"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{scenario}.{clause}.json").write_text(stable_json(payload))


def main(archive: Path, facts_source: str = "expected") -> dict:
    assert facts_source == "expected", f"источник фактов {facts_source!r} появится в задаче 24"
    archive = Path(archive)
    ds_hash, input_dir = extract_archive(archive)
    print(f"dataset_hash: {ds_hash}", flush=True)
    wd = workdir(ds_hash)

    # Скелет — как можно раньше: без шаблона нельзя построить даже его, всё
    # остальное (леджер, индекс, расчёт) уже падает поверх валидного файла.
    inputs = find_inputs(input_dir)
    template = json.loads(inputs["template"].read_text())
    answers: dict = skeleton(template["answers"])
    sub = {**SUBMISSION_META, "answers": answers}  # answers — та же ссылка, правки видны в sub
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

    # Диагностика 5.6: общее число непустых улик и доля на коэффициентных
    # метриках — резкий рост второй цифры значит, что D собрано слишком широко.
    emitted = ratio_emitted = 0
    # Посчитанные ярусом dsl пары (направление, actual) — медианная ступень
    # лестницы для ячеек без прочитанного порога.
    computed: list[tuple[str, float]] = []
    for scenario in targets:
        try:
            # Внутри try: даже единственная точка чтения фактов не имеет права
            # уронить цикл — сценарий уйдёт лестницей, остальные посчитаются.
            facts = _facts_of(scenario)
            raw, rows, fx_alarms = load_rows(
                scenario, scenario_rows, index, facts, _donor_rates(targets, scenario)
            )
        except Exception as exc:  # fail-open: ячейки приходят лестницей без строк
            print(f"ALARM scenario_failed {scenario}: {exc!r}", flush=True)
            for clause in sorted(template["answers"][scenario]):
                cell, trace = _spec_only_fallback(scenario, clause, computed, exc)
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
            cov = _write_borrower_trace(wd, scenario, rows, template["answers"][scenario], facts_source)
            if cov["alarm"] != "none":
                share = f"{cov['other_share']:.4f}"
                print(f"ALARM category_coverage {scenario}: {cov['alarm']} other_share={share}", flush=True)
        except Exception as exc:
            print(f"ALARM borrower_trace_failed {scenario}: {exc!r}", flush=True)
        for clause in sorted(template["answers"][scenario]):
            trace = {"scenario": scenario, "clause": clause}
            cell = answers[scenario][clause]  # скелет — фолбэк последней инстанции
            try:
                try:
                    cellspec_or_error: object = legacy_spec_to_cellspec(SPECS[scenario][clause])
                except Exception as exc:
                    cellspec_or_error = exc
                cell, trace = run_cell(scenario, clause, raw, facts, cellspec_or_error, computed)
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
    return answers


if __name__ == "__main__":
    from score import score as _score

    answers = main(Path(sys.argv[1]))
    gt_path = Path("dataset/agentic-bank-public/ground_truth.json")
    if gt_path.exists():
        _score(answers, json.loads(gt_path.read_text())["scenarios"])
