"""Сборка submission.json и скоринг против ground_truth (для публичного набора)."""

import copy
import json
import sys

sys.path.insert(0, "solution")
from covenants import DRIVERS, M
from engine import inflow, load
from engine import norm as norm_cp

sys.path.insert(0, "eval")
from expected_extraction import FACTS, SPECS


def verdict(actual, direction, limit):
    if direction == "max":
        return "BREACH" if actual > limit else "COMPLIANT"
    return "BREACH" if actual < limit else "COMPLIANT"


def evaluate(scenario, clause, rows, f):
    spec = SPECS[scenario][clause]
    name, direction, limit = spec[0], spec[1], spec[2]
    opts = spec[3] if len(spec) > 3 else {}
    actual = M[name](rows, f)
    if "trigger_financing" in opts and inflow(rows, "FINANCING") <= opts["trigger_financing"]:
        return "COMPLIANT", actual  # springing-тест не сработал
    return verdict(actual, direction, limit), actual


def find_evidence(scenario, clause, status, rows, f):
    """Улика — единственная операция, определяющая вердикт.

    Два случая: (1) ограничиваемая величина состоит ровно из одной операции;
    (2) решение аудитора/казначейства по конкретной операции переворачивает
    вердикт при откате.
    """
    if status != "BREACH":
        return None
    metric = SPECS[scenario][clause][0]
    driver = DRIVERS.get(metric)
    if driver:
        drivers = driver(rows, f)
        if len(drivers) == 1:
            return drivers[0]["id"]
    # (2) откат документального решения по конкретной операции
    for i in range(len(f.get("reclass", []))):
        alt = copy.deepcopy(f)
        item = alt["reclass"].pop(i)
        if not _flips(scenario, clause, status, alt, f):
            continue
        for r in rows:
            if item.get("txn") == r["id"]:
                return r["id"]
            cp = item.get("counterparty")
            if cp and norm_cp(cp) == norm_cp(r["cp"]):
                return r["id"]
    for txn in list(f.get("exclude", [])) + list(f.get("amount_override", {})):
        alt = copy.deepcopy(f)
        alt["exclude"] = [t for t in alt.get("exclude", []) if t != txn]
        alt["amount_override"] = {k: v for k, v in alt.get("amount_override", {}).items() if k != txn}
        if _flips(scenario, clause, status, alt, f):
            return txn
    return None


def _flips(scenario, clause, status, alt, original):
    FACTS[scenario] = alt
    try:
        alt_rows, _ = load(scenario)
        return evaluate(scenario, clause, alt_rows, alt)[0] != status
    except ZeroDivisionError:
        return False
    finally:
        FACTS[scenario] = original


def solve():
    answers = {}
    for scenario in SPECS:
        rows, f = load(scenario)
        cells = {}
        for clause in ("6.1", "6.2", "6.3"):
            status, actual = evaluate(scenario, clause, rows, f)
            cells[clause] = {
                "status": status,
                "actual": round(abs(actual), 2),
                "evidence_txn_id": find_evidence(scenario, clause, status, rows, f),
            }
        answers[scenario] = cells
    return answers


def score(answers, gt_path="dataset/agentic-bank-public/ground_truth.json"):
    gt = json.load(open(gt_path))["scenarios"]
    total = 0.0
    print(f"{'ячейка':<9} {'наш статус':<10} {'ключ':<10} {'наш actual':>15} {'ключ actual':>15}  балл")
    for sc in sorted(answers):
        for cl in ("6.1", "6.2", "6.3"):
            got, key = answers[sc][cl], gt[sc]["covenants"][cl]
            pts = 0.0
            if got["status"] == key["status"]:
                pts += 0.50
                e = abs(got["actual"] - key["actual"]) / abs(key["actual"]) if key["actual"] else 0
                scale = max(0.0, 1 - e / 0.05)
                pts += 0.30 * scale
                if key["evidence_txn_id"] is None:
                    pts += 0.20 * scale
                elif got["evidence_txn_id"] == key["evidence_txn_id"]:
                    pts += 0.20
            flag = "" if pts > 0.99 else ("  <<<" if pts < 0.5 else "  <")
            print(
                f"{sc + ' ' + cl:<9} {got['status']:<10} {key['status']:<10} "
                f"{got['actual']:>15,.2f} {key['actual']:>15,.2f}  {pts:.2f}{flag}"
            )
            total += pts
    print(f"\nИТОГО: {total:.2f} / 36.00  ({total / 36 * 100:.1f}%)")
    return total


if __name__ == "__main__":
    a = solve()
    json.dump(
        {"team": "", "contact_email": "", "model": "", "answers": a},
        open("solution/submission.json", "w"),
        indent=2,
    )
    score(a)
