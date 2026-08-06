"""Harness: скелет-первым submission, fail-open на ячейку, трейс.

Submission пишется задом наперёд (раздел 6): сначала на диск кладётся
полностью заполненный фолбэками скелет, каждая посчитанная ячейка
перезаписывает свою — на любой секунде прогона на диске валидный файл.
Скелет строится сразу после распаковки и чтения шаблона, до леджера и
индекса: всё, что может упасть после этого, оставляет на диске валидный
файл вместо пустоты.

Вычислительное ядро в solve_cell — интерпретатор DSL по шаблонам метрик;
спеки и факты пока эталонные (мост legacy_spec_to_cellspec), задача 24
подменит их на извлечённые, не трогая harness.
"""

import copy
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "solution")
sys.path.insert(0, "eval")

from expected_extraction import FACTS, SPECS

from covenants import DRIVERS, METRIC_CATEGORIES
from dsl import parse
from engine import norm as norm_cp
from engine import prepare_rows, select_rows, sign_divergence
from fx import coverage_alarms, to_usd
from interp import Ctx, check_trigger, evaluate, verdict
from ledger import dirty_rows_of, extract_archive, find_inputs, load_ledger, rows_of
from scindex import INDEX_VERSION, build_index
from stages import artifact
from templates import TEMPLATES
from util import OUT, ROOT, q2, stable_json, workdir

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
    готовый doc_facts.
    """
    return _with_doc_facts(FACTS[scenario])


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
        "direction": direction,
        "limit": Decimal(str(limit)),
        "trigger_ast": trigger,
    }


def _cell_verdict(scenario, clause, rows, facts):
    """(status, res, alarms) ячейки интерпретатором. Триггер решает, применяется
    ли тест, но вычисление метрики происходит всегда (5.7)."""
    cellspec = legacy_spec_to_cellspec(SPECS[scenario][clause])
    ctx = Ctx(rows=rows, facts=facts)
    res = evaluate(cellspec["metric_ast"], ctx)
    if not check_trigger(cellspec["trigger_ast"], ctx):
        return "COMPLIANT", res, sorted(res.flags)  # springing-тест не сработал
    status, alarms = verdict(res, cellspec["direction"], cellspec["limit"])
    return status, res, alarms


def _flips(scenario, clause, status, alt, raw):
    """Контрфактуал считается на тех же сырых строках с изменёнными фактами —
    без подмены глобального FACTS: скрытое состояние сломало бы детерминизм
    при параллельном счёте сценариев (задача 24)."""
    return _cell_verdict(scenario, clause, prepare_rows(raw, alt), alt)[0] != status


def _find_evidence(scenario, clause, status, rows, facts, raw):
    """Улика — единственная операция, определяющая вердикт.

    Два случая: (1) ограничиваемая величина состоит ровно из одной операции;
    (2) решение аудитора/казначейства по конкретной операции переворачивает
    вердикт при откате. Перебор откатов отсортирован — порядок улик не должен
    зависеть от порядка ключей в фактах (раздел 3).
    """
    if status != "BREACH":
        return None
    metric = SPECS[scenario][clause][0]
    driver = DRIVERS.get(metric)
    if driver:
        drivers = driver(rows, facts)
        if len(drivers) == 1:
            return drivers[0]["txn_id"]
    for i in range(len(facts.get("reclass", []))):
        alt = copy.deepcopy(facts)
        item = alt["reclass"].pop(i)
        if not _flips(scenario, clause, status, alt, raw):
            continue
        for r in rows:
            if item.get("txn") == r["txn_id"]:
                return r["txn_id"]
            cp = item.get("counterparty")
            if cp and norm_cp(cp) == norm_cp(r["counterparty"]):
                return r["txn_id"]
    for txn in sorted(list(facts.get("exclude", [])) + list(facts.get("amount_override", {}))):
        alt = copy.deepcopy(facts)
        alt["exclude"] = [t for t in alt.get("exclude", []) if t != txn]
        alt["amount_override"] = {k: v for k, v in alt.get("amount_override", {}).items() if k != txn}
        if _flips(scenario, clause, status, alt, raw):
            return txn
    return None


def solve_cell(scenario: str, clause: str, rows: list, facts: dict, raw: list) -> dict:
    """Одна ячейка ответа. Точка подмены ядра задачами фаз 1–2: сигнатура и
    форма результата фиксированы, содержимое — нет.

    Модуль значения берётся только здесь, при записи в submission: вердикт
    выше считается со знаком (interp.verdict)."""
    status, res, alarms = _cell_verdict(scenario, clause, rows, facts)
    return {
        "status": status,
        "actual": q2(abs(res.value)),
        "evidence_txn_id": _find_evidence(scenario, clause, status, rows, facts, raw),
        "_alarms": alarms,  # снимается перед записью в submission, уходит в трейс
    }


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


def _prior_status() -> str:
    """Самый частый статус по публичному набору — фолбэк ячейки, которую не
    удалось посчитать. Ключи сортируются: при равенстве частот выбор не должен
    зависеть от порядка в JSON."""
    p = json.loads((ROOT / "eval" / "prior.json").read_text())["global"]
    return max(sorted(p), key=lambda k: p[k])


def skeleton(template_answers: dict) -> dict:
    status = _prior_status()
    return {
        sc: {cl: {"status": status, "actual": 1.0, "evidence_txn_id": None} for cl in cells}
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
    for alarm in index["alarms"]:
        print(f"ALARM {alarm}", flush=True)

    for scenario in targets:
        facts = _facts_of(scenario)
        try:
            raw, rows, fx_alarms = load_rows(
                scenario, scenario_rows, index, facts, _donor_rates(targets, scenario)
            )
        except Exception as exc:  # fail-open: сценарий целиком остаётся скелетом
            print(f"ALARM scenario_failed {scenario}: {exc!r}", flush=True)
            for clause in sorted(template["answers"][scenario]):
                _write_trace(
                    wd,
                    scenario,
                    clause,
                    {"scenario": scenario, "clause": clause, "path": "legacy", "error": repr(exc)},
                )
            continue
        for alarm in fx_alarms:
            print(f"ALARM {alarm}", flush=True)
        for clause in sorted(template["answers"][scenario]):
            trace = {"scenario": scenario, "clause": clause, "path": "legacy"}
            if fx_alarms:
                trace["fx_alarms"] = fx_alarms
            # Знак расходной категории: дефолт out, а расхождение с net значит
            # сторно внутри читаемой категории — на приватном наборе такие
            # ячейки видны сразу, а не после разбора расхождения в баллах.
            divergence = sign_divergence(rows, METRIC_CATEGORIES.get(SPECS[scenario][clause][0], []))
            if divergence:
                trace["sign_divergence"] = divergence
            try:
                cell = solve_cell(scenario, clause, rows, facts, raw)
                cell_alarms = cell.pop("_alarms", [])
                if cell_alarms:
                    trace["alarms"] = cell_alarms
                trace["cell"] = cell
            except Exception as exc:  # fail-open: ячейка остаётся фолбэком
                trace["error"] = repr(exc)
                print(f"ALARM cell_failed {scenario} {clause}: {exc!r}", flush=True)
                cell = answers[scenario][clause]
            answers[scenario][clause] = cell
            dump_submission(sub, template["answers"])
            _write_trace(wd, scenario, clause, trace)
    return answers


if __name__ == "__main__":
    from score import score as _score

    answers = main(Path(sys.argv[1]))
    gt_path = Path("dataset/agentic-bank-public/ground_truth.json")
    if gt_path.exists():
        _score(answers, json.loads(gt_path.read_text())["scenarios"])
