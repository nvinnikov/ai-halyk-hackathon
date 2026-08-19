"""Скор нашего сабмишна против ПРОКСИ-ключа приватного набора.

Ключ — не истина: это ответ чужой команды с ~93–95% по таблице лидеров, наш
реальный скор на приватном наборе — 70.99% (см. `docs/ops/private-set-
postmortem.md`). Метрика направленная: она годится сравнивать прогон с
прогоном одного и того же кода, а не объявлять отдельную ячейку неверной —
расхождение с этим ключом может быть и нашей ошибкой, и ошибкой ключа.

Конкретные числа скора здесь намеренно не приводятся: они меняются с каждой
правкой конвейера и стареют быстрее докстринга. Актуальное значение — прогнать
самому (`LLM_OFFLINE=1 LLM_PROVIDER=gemini ./run.sh
6a7819a8cb7d3480322468.zip && make private-score`) либо смотреть в
`docs/ops/private-set-postmortem.md`, раздел «После починок» — там число всегда
идёт с датой и коммитом, к которым привязано.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, "solution")

from score import _cell_points  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KEY_PATH = ROOT / "eval" / "private_proxy_key.json"


def load_key(path: Path | None = None) -> dict:
    data = json.loads((path or KEY_PATH).read_text())
    return data["scenarios"]


def _cell_components(got: dict, k: dict) -> tuple[float, float, float]:
    """Раскладывает балл ячейки на (status, actual, evidence) по формуле CASE.ru.md.

    Считается напрямую по разделу 4, а не вычитанием из `_cell_points`: при
    частичном `scale` вычитание из суммы не отделяет actual от evidence.
    """
    if got.get("status") != k["status"]:
        return 0.0, 0.0, 0.0
    status_pts = 0.50
    actual = got.get("actual")
    if isinstance(actual, int | float) and not isinstance(actual, bool):
        if k["actual"]:
            e = abs(actual - k["actual"]) / abs(k["actual"])
        else:
            e = 0.0 if actual == k["actual"] else 1.0
        scale = max(0.0, 1 - e / 0.05)
    else:
        scale = 0.0
    actual_pts = 0.30 * scale
    if k["evidence_txn_id"] is None:
        evidence_pts = 0.20 * scale
    elif got.get("evidence_txn_id") == k["evidence_txn_id"]:
        evidence_pts = 0.20
    else:
        evidence_pts = 0.0
    return status_pts, actual_pts, evidence_pts


def score_private(answers: dict, key: dict) -> dict:
    total = status_pts = actual_pts = evidence_pts = 0.0
    cells = 0
    rows = []
    for sc in sorted(key):
        for cl in sorted(key[sc]["covenants"]):
            k = key[sc]["covenants"][cl]
            got = answers.get(sc, {}).get(cl, {})
            pts = _cell_points(got, k)
            s_pts, a_pts, e_pts = _cell_components(got, k)
            assert abs((s_pts + a_pts + e_pts) - pts) < 1e-9, (
                f"{sc} {cl}: разложение на компоненты ({s_pts + a_pts + e_pts:.6f}) "
                f"разошлось со счётом _cell_points ({pts:.6f})"
            )
            cells += 1
            total += pts
            status_pts += s_pts
            actual_pts += a_pts
            evidence_pts += e_pts
            rows.append((f"{sc} {cl}", got, k, pts))
    return {
        "total": total,
        "cells": cells,
        "status_pts": status_pts,
        "actual_pts": actual_pts,
        "evidence_pts": evidence_pts,
        "rows": rows,
    }


def main() -> int:
    sub = json.loads(Path(sys.argv[1]).read_text())
    answers = sub["answers"] if "answers" in sub else sub
    res = score_private(answers, load_key())
    for name, got, k, pts in res["rows"]:
        mark = "" if pts > 0.99 else ("  <<<" if pts < 0.5 else "  <")
        print(
            f"{name:<9} {str(got.get('status')):<9}/{k['status']:<9} "
            f"{str(got.get('actual')):>16}/{k['actual']:>16,.2f}  "
            f"{str(got.get('evidence_txn_id')):<14}/{str(k['evidence_txn_id']):<14} "
            f"{pts:.2f}{mark}"
        )
    print(
        f"\nИТОГО {res['total']:.2f} / {float(res['cells']):.2f} = {100 * res['total'] / res['cells']:.2f}%"
    )
    print(
        f"  status {res['status_pts']:.2f}/{0.5 * res['cells']:.2f}  "
        f"actual {res['actual_pts']:.2f}/{0.3 * res['cells']:.2f}  "
        f"evidence {res['evidence_pts']:.2f}/{0.2 * res['cells']:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
