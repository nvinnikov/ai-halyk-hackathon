"""Инварианты (7.4): каждая функция возвращает список провалов [{check, detail}].

Дешёвые детерминированные проверки, ловящие почти все катастрофы: неверную
сшивку досье, потерянные строки леджера, пропущенную улику, разъехавшийся
шаблон. Каждая функция читает уже посчитанные артефакты — сама она ничего
не считает и не трогает LLM. run_invariants кормит их артефактами прогона
(index.json, wd/trace/*.json, route/, dossier/) и по-заёмщицкими данными,
пересобранными заново через solve.scenario_inputs — сравнение с уже
записанным submission не тавтологично именно потому, что вход пересчитан
независимо от него.
"""

import json
import math
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "solution")

import solve
from dsl import Agg, walk
from engine import prepare_rows, tokens
from mutations import _isolated_solve_out
from taxonomy import coverage_report

_MARGIN_DEFAULT = 0.10


def _fail(check, **detail):
    return {"check": check, **detail}


# --- по-заёмщицкие проверки (принимают уже отобранные строки/факты) --------


def check_reclass_applied(sc, raw_rows, facts):
    """Каждая реклассификация из фактов обязана задеть хотя бы одну строку —
    иначе имя контрагента в документе разошлось с леджером (5.2/5.3)."""
    fails = []
    for rc in facts.get("reclass", []):
        hit = any(
            rc.get("txn") == r["txn_id"]
            or (rc.get("counterparty") and tokens(rc["counterparty"]) == tokens(r["counterparty"]))
            for r in raw_rows
        )
        if not hit:
            fails.append(_fail("reclass_applied", scenario=sc, reclass=rc))
    return fails


def check_sum_conservation(sc, raw_rows, facts):
    """Честная сверка: сумма модулей подготовленных строк (prepare_rows)
    сравнивается с суммой, независимо посчитанной прямо из raw + фактов —
    не с числом, которое дал бы тот же prepare_rows на тех же данных
    (тавтология из старого замечания). Расхождение — потерянная или лишняя
    строка в применении реклассификации/исключения/исправления суммы."""
    prepared = prepare_rows(raw_rows, facts)
    got = sum((abs(r["amt"]) for r in sorted(prepared, key=lambda x: x["txn_id"])), Decimal(0))

    excluded = set(facts.get("exclude", []))
    overrides = facts.get("amount_override", {})
    want = Decimal(0)
    for r in sorted(raw_rows, key=lambda x: x["txn_id"]):
        if r["txn_id"] in excluded:
            continue
        amt = r["amt"]
        if amt is None:
            override = overrides.get(r["txn_id"])
            if override is None:
                continue  # не восстановлена документом — не входит в ожидание
            amt = Decimal(str(override))
        want += abs(amt)

    if got != want:
        return [_fail("sum_conservation", scenario=sc, got=str(got), want=str(want))]
    return []


def check_other_share(sc, rows, referenced):
    rep = coverage_report(rows, referenced)
    if rep["alarm"] == "critical":
        return [_fail("other_share_critical", scenario=sc, report=rep)]
    return []


def check_dirty_rows_recovered(sc, dirty_rows, facts):
    """Целевая строка с непарсимой суммой обязана иметь amount_override в
    фактах — иначе она молча выпала из расчёта (research 13-15: единственный
    реальный вид грязных данных на публичном наборе — это ровно такая
    строка). Фоновые dirty-строки сюда не передаются вызывающим кодом."""
    overrides = facts.get("amount_override", {})
    return [
        _fail("dirty_row_unrecovered", scenario=sc, txn=r["txn_id"])
        for r in sorted(dirty_rows, key=lambda x: x["txn_id"])
        if r["txn_id"] not in overrides
    ]


# --- проверки над submission -------------------------------------------------


def check_actuals_finite(answers):
    return [
        _fail("actual_finite", scenario=sc, clause=cl)
        for sc, cells in sorted(answers.items())
        for cl, cell in sorted(cells.items())
        if not isinstance(cell["actual"], int | float) or not math.isfinite(cell["actual"])
    ]


def check_template_keys(answers, template_answers):
    got = {(sc, cl) for sc, cells in answers.items() for cl in cells}
    want = {(sc, cl) for sc, cells in template_answers.items() for cl in cells}
    if got != want:
        return [_fail("template_keys", missing=sorted(want - got), extra=sorted(got - want))]
    return []


# --- проверки над трейсами ячеек (wd/trace/<сценарий>.<пункт>.json) --------


def check_evidence_provenance(answers, traces):
    """Выданная улика обязана быть кандидатом из D (evidence.find) с непустым
    decision_type — иначе она подменена вкладчиком, не документом."""
    fails = []
    for sc, cells in sorted(answers.items()):
        for cl, cell in sorted(cells.items()):
            ev = cell["evidence_txn_id"]
            if ev is None:
                continue
            cands = traces.get((sc, cl), {}).get("evidence", [])
            if not any(c["txn"] == ev and c.get("decision_type") for c in cands):
                fails.append(_fail("evidence_provenance", scenario=sc, clause=cl, txn=ev))
    return fails


def check_breach_evidence(answers, traces):
    """BREACH и ровно один переворачивающий кандидат в D → улика не должна
    быть null (пропущенная улика — evidence.find недосчитал)."""
    fails = []
    for sc, cells in sorted(answers.items()):
        for cl, cell in sorted(cells.items()):
            flippers = {c["txn"] for c in traces.get((sc, cl), {}).get("evidence", []) if c.get("flipped")}
            if cell["status"] == "BREACH" and len(flippers) == 1 and cell["evidence_txn_id"] is None:
                fails.append(_fail("breach_evidence_missing", scenario=sc, clause=cl))
    return fails


def _fallback_rate(traces: dict) -> float:
    cell_traces = [t for t in traces.values() if "tier" in t]
    if not cell_traces:
        return 0.0
    fell_back = sum(1 for t in cell_traces if t.get("tier", 0) > 0)
    return fell_back / len(cell_traces)


def check_fallback_rate(traces: dict, baseline_path: Path | None, margin: float = _MARGIN_DEFAULT):
    """Доля ячеек, посчитанных не ярусом dsl (tier > 0). Все прочие 13
    проверок зелёные и на submission, целиком собранном из фолбэков —
    сломанное извлечение на приватном наборе они не заметят, этот инвариант
    заметит. Потолок = baseline публичного extracted-прогона (задача 30,
    eval/public_baseline.json) + запас; baseline ещё не зафиксирован —
    проверка пропускается, а не проваливается (недоступный артефакт)."""
    if baseline_path is None or not baseline_path.exists():
        return []
    baseline = json.loads(baseline_path.read_text()).get("fallback_rate")
    if baseline is None:
        return []
    rate = _fallback_rate(traces)
    ceiling = baseline + margin
    if rate > ceiling:
        return [_fail("fallback_rate", rate=rate, ceiling=ceiling)]
    return []


def _baseline_path() -> Path:
    return Path(__file__).resolve().parent / "public_baseline.json"


def _print_fallback_rate_status(baseline_path: Path) -> None:
    """Видимый сигнал пропуска check_fallback_rate. Тихий [] от отсутствия
    baseline неотличим от «инвариант проверен и чист» — разрыв с
    fail-open-конвенцией остальной кодовой базы (solve печатает ALARM на
    каждый деградировавший путь, не молчит). check_fallback_rate сам
    остаётся чистой функцией без побочных эффектов — печать вынесена
    сюда, вызывающий (main) решает, когда её показать."""
    if not baseline_path.exists():
        print(
            f"ALARM fallback_rate_baseline_missing: {baseline_path} отсутствует — "
            "инвариант пропущен (появится в задаче 30)",
            flush=True,
        )


def check_fx_alarms(all_fx_alarms):
    return [_fail("fx", alarm=a) for a in all_fx_alarms if str(a.get("kind", "")).startswith("fx_uncovered")]


# --- проверки над index.json -------------------------------------------------


def check_index_unique(index):
    return [_fail("index", alarm=a) for a in index["alarms"]]


def check_background_share(index):
    share = index["background"]["row_share"]
    if not 0.3 <= share <= 0.8:
        return [_fail("background_share", share=share)]
    return []


# --- проверки над route/dossier-артефактами ----------------------------------


def check_single_agreement(route_docs, target_accounts):
    """Не по dossier.docs: там _pick_active гарантированно оставляет ровно
    один (или ноль) документ, и проверка после фильтрации никогда не
    срабатывает даже при настоящей поломке апстрима. Здесь фильтр редакции
    (edition != "superseded") реализован независимо от dossier._pick_active —
    второй счёт тем же критерием, не переиспользующий её код, поэтому
    способен разойтись с dossier.docs, если _pick_active выбрала не ту
    редакцию. superseded — штатный случай (перевыпущенный договор), его не
    считаем; n == 0 — счёт остался без действующей редакции; n > 1 —
    несколько действующих одновременно (неотфильтрованная редакция).

    Литерал "superseded" — не общий символ, а копия значения из
    solution/dossier.py (_EDITION_RANK и сравнение в _pick_active): если
    там появится новое имя редакции для «неактивна», эту строку придётся
    поправить синхронно."""
    counts = {acc: 0 for acc in target_accounts}
    for d in route_docs:
        acc = d.get("account_id")
        if (
            acc in counts
            and d.get("doc_type") == "agreement"
            and not d.get("quarantined")
            and d.get("edition") != "superseded"
        ):
            counts[acc] += 1
    return [_fail("single_agreement", account=acc, n=n) for acc, n in sorted(counts.items()) if n != 1]


def _doc_tokens(text: str) -> frozenset[str]:
    """Токены шапки документа (первые 500 символов), порог длины ≥ 4 — уже
    отличается от engine.tokens (≥ 3, без юрформ): здесь достаточно
    отфильтровать союзы/предлоги, юрформа компании — тоже сигнал совпадения
    имени, вычёркивать её не нужно."""
    words = re.split(r"[^\w]+", text[:500].lower())
    return frozenset(w for w in words if len(w) >= 4)


def check_dossier_binding(dossiers):
    """У каждого сценария есть договор; заёмщик в договоре упомянут и в
    KYC — иначе это сшивка не того досье. KYC в досье может не быть
    (недоступный артефакт) — тогда сверять имя не с чем, это не провал."""
    fails = []
    for d in dossiers:
        by_type = {x["doc_type"]: x for x in d["docs"]}
        if "agreement" not in by_type:
            fails.append(_fail("dossier_binding", account=d["account_id"], missing="agreement"))
            continue
        kyc = by_type.get("kyc")
        if kyc is None:
            continue
        agr_tok = _doc_tokens(by_type["agreement"]["text"])
        kyc_tok = _doc_tokens(kyc["text"])
        if not (agr_tok & kyc_tok):
            fails.append(_fail("dossier_binding_mismatch", account=d["account_id"]))
    return fails


# --- сборка -------------------------------------------------------------------


def _referenced_categories(scenario: str) -> set[str]:
    """Категории, читаемые метриками всех пунктов сценария — для
    check_other_share. Источник спек тот же эталон, что и borrower-трейс в
    expected-режиме (5.3 для extracted подключит задача 24-мост)."""
    refs: set[str] = set()
    for _clause, spec in solve.SPECS.get(scenario, {}).items():
        try:
            cs = solve.legacy_spec_to_cellspec(spec)
        except Exception:
            continue
        refs |= {n.category for n in walk(cs["metric_ast"]) if isinstance(n, Agg)}
    return refs


def run_invariants(archive: Path, wd: Path, answers: dict, template_answers: dict) -> list[dict]:
    """Собирает данные из артефактов прогона и кормит проверки; недоступный
    артефакт (route/dossier ещё не построены — expected-режим их не пишет
    вовсе) пропускается, а не роняет остальные проверки."""
    fails: list[dict] = []

    index = json.loads((wd / "index.json").read_text())
    fails += check_index_unique(index)
    fails += check_background_share(index)
    fails += check_template_keys(answers, template_answers)
    fails += check_actuals_finite(answers)

    traces: dict[tuple[str, str], dict] = {}
    for p in sorted((wd / "trace").glob("*.json")):
        if p.stem.endswith(".borrower"):
            continue  # <сценарий>.borrower.json — не пер-ячейковый трейс
        sc, cl = p.stem.split(".", 1)
        traces[(sc, cl)] = json.loads(p.read_text())
    fails += check_evidence_provenance(answers, traces)
    fails += check_breach_evidence(answers, traces)

    all_fx_alarms = [a for t in traces.values() for a in t.get("fx_alarms", [])]
    fails += check_fx_alarms(all_fx_alarms)

    fails += check_fallback_rate(traces, _baseline_path())

    route_dir = wd / "route"
    if route_dir.is_dir():
        route_docs = [json.loads(p.read_text()) for p in sorted(route_dir.glob("*.json"))]
        fails += check_single_agreement(route_docs, sorted(index["account_to_scenario"]))

    dossier_dir = wd / "dossier"
    if dossier_dir.is_dir():
        dossiers = [json.loads(p.read_text()) for p in sorted(dossier_dir.glob("*.json"))]
        fails += check_dossier_binding(dossiers)

    # По-заёмщицкие проверки (правка 1): вход пересобирается заново из
    # архива через solve.scenario_inputs, а не переиспользует то, что уже
    # легло в answers/traces — иначе сверка была бы тавтологией.
    targets = sorted(template_answers)
    _, input_dir = solve.extract_archive(archive)
    ledger_art = solve.load_ledger(wd, input_dir, target_scenarios=targets)
    for sc in targets:
        raw, facts = solve.scenario_inputs(archive, sc)
        fails += check_reclass_applied(sc, raw, facts)
        fails += check_sum_conservation(sc, raw, facts)
        prepared = prepare_rows(raw, facts)
        fails += check_other_share(sc, prepared, _referenced_categories(sc))
        acc = index["scenario_to_account"].get(sc)
        dirty = [r for r in ledger_art["dirty"] if r["account_id"] == acc]
        fails += check_dirty_rows_recovered(sc, dirty, facts)

    return fails


def _collect_report_alarms(wd: Path) -> list[dict]:
    """Алярмы, разбросанные по артефактам прогона, — для отчёта main(), не
    для проверок (те читают то же самое напрямую по месту).

    facts/specs обязательны наравне с route/dossier: facts_extraction_failed/
    specs_extraction_failed запекаются ВНУТРЬ build()-результата
    stages.artifact и кэшируются под текущей версией стадии — см.
    .superpowers/sdd/2026-08-06-halyk-pipeline/recovery-playbook.md. Без этих
    двух каталогов отчёт мог бы выглядеть чистым на отравленном work/<hash>."""
    alarms: list[dict] = []
    index_path = wd / "index.json"
    if index_path.exists():
        alarms += json.loads(index_path.read_text())["alarms"]
    for p in sorted((wd / "trace").glob("*.json")) if (wd / "trace").is_dir() else []:
        payload = json.loads(p.read_text())
        alarms += payload.get("alarms", [])
        alarms += payload.get("fx_alarms", [])
    for sub in ("route", "dossier", "facts", "specs"):
        d = wd / sub
        if d.is_dir():
            for p in sorted(d.glob("*.json")):
                alarms += json.loads(p.read_text()).get("alarms", [])
    return alarms


def main(archive, facts_source: str = "expected") -> int:
    """Прогоняет solve.main, затем run_invariants; печатает отчёт (провалы +
    алярмы, собранные из трейсов и артефактов), возвращает число провалов.

    facts_source по умолчанию "expected" — а не "extracted", как у
    solve.main: инварианты должны быть безопасны прогнать в одиночку без
    LLM-бюджета под рукой; вызывающий явно передаёт "extracted", когда
    бюджет прогрет (см. CLI ниже)."""
    archive = Path(archive)
    ds_hash, input_dir = solve.extract_archive(archive)
    wd = solve.workdir(ds_hash)
    with _isolated_solve_out(archive):
        answers = solve.main(archive, facts_source=facts_source)
    template = json.loads(solve.find_inputs(input_dir)["template"].read_text())

    for alarm in _collect_report_alarms(wd):
        print(f"ALARM {alarm}", flush=True)
    _print_fallback_rate_status(_baseline_path())

    fails = run_invariants(archive, wd, answers, template["answers"])
    for f in fails:
        print(f"INVARIANT_FAIL {f['check']}: {f}", flush=True)
    print(f"invariants: {len(fails)} провалов", flush=True)
    return len(fails)


if __name__ == "__main__":
    _archive = Path(sys.argv[1])
    _facts_source = sys.argv[2] if len(sys.argv) > 2 else "expected"
    _n = main(_archive, facts_source=_facts_source)
    sys.exit(1 if _n else 0)
