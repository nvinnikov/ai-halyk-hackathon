"""Скорер по официальной формуле (CASE.ru.md, раздел 4), без вариантов.

Веса ячеек по сложности неизвестны, поэтому итог — сырая сумма, сравнимая
только сама с собой между прогонами. evidence печатается обязательно: иначе
расхождения в ячейках с непустым ключом не видны.
"""


def _cell_points(got: dict, key: dict) -> float:
    if got.get("status") != key["status"]:
        return 0.0
    pts = 0.50
    actual = got.get("actual")
    if isinstance(actual, int | float) and not isinstance(actual, bool):
        if key["actual"]:
            e = abs(actual - key["actual"]) / abs(key["actual"])
        else:
            e = 0.0 if actual == key["actual"] else 1.0
        scale = max(0.0, 1 - e / 0.05)
    else:
        scale = 0.0
    pts += 0.30 * scale
    if key["evidence_txn_id"] is None:
        pts += 0.20 * scale
    elif got.get("evidence_txn_id") == key["evidence_txn_id"]:
        pts += 0.20
    return pts


def score(answers: dict, gt_scenarios: dict, verbose: bool = True) -> float:
    total = 0.0
    n = 0
    if verbose:
        print(f"{'ячейка':<9} {'статус':<19} {'actual (наш/ключ)':>28}  {'улика (наша/ключ)':<28} балл")
    for sc in sorted(gt_scenarios):
        for cl in sorted(gt_scenarios[sc]["covenants"]):
            key = gt_scenarios[sc]["covenants"][cl]
            got = answers.get(sc, {}).get(cl, {})
            pts = _cell_points(got, key)
            total += pts
            n += 1
            if verbose:
                mark = "" if pts > 0.99 else ("  <<<" if pts < 0.5 else "  <")
                ga = got.get("actual")
                ga_s = f"{ga:,.2f}" if isinstance(ga, int | float) else str(ga)
                print(
                    f"{sc + ' ' + cl:<9} {str(got.get('status')):<9}/{key['status']:<9} "
                    f"{ga_s:>13}/{key['actual']:>13,.2f}  "
                    f"{str(got.get('evidence_txn_id')):<13}/{str(key['evidence_txn_id']):<13} "
                    f"{pts:.2f}{mark}"
                )
    if verbose:
        print(f"\nИТОГО: {total:.2f} / {float(n):.2f}")
    return total
