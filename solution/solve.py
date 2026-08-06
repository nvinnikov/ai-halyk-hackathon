"""Harness: скелет-первым submission, fail-open на ячейку, трейс.

Submission пишется задом наперёд (раздел 6): сначала на диск кладётся
полностью заполненный фолбэками скелет, каждая посчитанная ячейка
перезаписывает свою — на любой секунде прогона на диске валидный файл.
Скелет строится сразу после распаковки и чтения шаблона, до леджера и
индекса: всё, что может упасть после этого, оставляет на диске валидный
файл вместо пустоты.

Вычислительное ядро в solve_cell пока легаси (engine + covenants на
эталонных фактах); фазы 1–2 подменяют его, не трогая harness.
"""

import copy
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "solution")
sys.path.insert(0, "eval")

from expected_extraction import FACTS, SPECS

from covenants import DRIVERS, M
from engine import inflow, load
from engine import norm as norm_cp
from ledger import extract_archive, find_inputs, load_ledger, rows_of
from scindex import INDEX_VERSION, build_index
from stages import artifact
from util import OUT, ROOT, q2, stable_json, workdir

SUBMISSION_META = {"team": "", "contact_email": "", "model": ""}


# --- легаси-ядро (заменяется задачами 15/16/24) ------------------------------


def _verdict(actual, direction, limit):
    if direction == "max":
        return "BREACH" if actual > limit else "COMPLIANT"
    return "BREACH" if actual < limit else "COMPLIANT"


def _evaluate(scenario, clause, rows, facts):
    spec = SPECS[scenario][clause]
    name, direction, limit = spec[0], spec[1], spec[2]
    opts = spec[3] if len(spec) > 3 else {}
    actual = M[name](rows, facts)
    if "trigger_financing" in opts and inflow(rows, "FINANCING") <= opts["trigger_financing"]:
        return "COMPLIANT", actual  # springing-тест не сработал
    return _verdict(actual, direction, limit), actual


def _flips(scenario, clause, status, alt, original):
    FACTS[scenario] = alt
    try:
        alt_rows, _ = load(scenario)
        return _evaluate(scenario, clause, alt_rows, alt)[0] != status
    except ZeroDivisionError:
        return False
    finally:
        FACTS[scenario] = original


def _find_evidence(scenario, clause, status, rows, facts):
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
            return drivers[0]["id"]
    for i in range(len(facts.get("reclass", []))):
        alt = copy.deepcopy(facts)
        item = alt["reclass"].pop(i)
        if not _flips(scenario, clause, status, alt, facts):
            continue
        for r in rows:
            if item.get("txn") == r["id"]:
                return r["id"]
            cp = item.get("counterparty")
            if cp and norm_cp(cp) == norm_cp(r["cp"]):
                return r["id"]
    for txn in sorted(list(facts.get("exclude", [])) + list(facts.get("amount_override", {}))):
        alt = copy.deepcopy(facts)
        alt["exclude"] = [t for t in alt.get("exclude", []) if t != txn]
        alt["amount_override"] = {k: v for k, v in alt.get("amount_override", {}).items() if k != txn}
        if _flips(scenario, clause, status, alt, facts):
            return txn
    return None


def solve_cell(scenario: str, clause: str, rows: list, facts: dict) -> dict:
    """Одна ячейка ответа. Точка подмены ядра задачами фаз 1–2: сигнатура и
    форма результата фиксированы, содержимое — нет."""
    status, actual = _evaluate(scenario, clause, rows, facts)
    return {
        "status": status,
        "actual": q2(Decimal(str(abs(actual)))),
        "evidence_txn_id": _find_evidence(scenario, clause, status, rows, facts),
    }


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
    for alarm in index["alarms"]:
        print(f"ALARM {alarm}", flush=True)

    for scenario in targets:
        try:
            rows, facts = load(scenario)  # легаси-загрузка; задача 11 заменит
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
        for clause in sorted(template["answers"][scenario]):
            trace = {"scenario": scenario, "clause": clause, "path": "legacy"}
            try:
                cell = solve_cell(scenario, clause, rows, facts)
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
