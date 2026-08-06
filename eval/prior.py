"""Эмпирический приор статусов из публичного ключа (5.7).

Приор — иерархия с деградацией: пункт → семья → глобальный. Пункт самый точный,
на приватном наборе с иными пунктами мягко откатывается к семье. Номер пункта —
признак статистики в eval/prior.json, не литерал в solution/; раздел 9 этого не запрещает.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "eval")
from expected_extraction import SPECS

GT = Path("dataset/agentic-bank-public/ground_truth.json")

_RATIO = {
    "icr",
    "capital_intensity",
    "sources_cover",
    "springing_leverage",
    "adj_ebitda_margin",
    "group_capex_to_ebitda",
    "tax_utility_to_ebitda",
    "revenue_cover_payroll_utilities",
    "insurance_cover",
}
_SHARE = {"related_share_revenue", "related_share_opex", "unrestricted_transfer_share"}


def metric_family(metric_name: str) -> str:
    """Определить семью метрики."""
    if metric_name in _RATIO:
        return "ratio"
    if metric_name in _SHARE:
        return "share"
    return "absolute"


def build_prior() -> dict:
    """Построить иерархию приора: пункт → семья → глобальный."""
    gt = json.loads(GT.read_text())["scenarios"]
    global_counts: dict[str, int] = defaultdict(int)
    by: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_clause: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for sc in sorted(gt):
        for cl in sorted(gt[sc]["covenants"]):
            status = gt[sc]["covenants"][cl]["status"]
            metric, direction = SPECS[sc][cl][0], SPECS[sc][cl][1]
            global_counts[status] += 1
            by[f"{direction}|{metric_family(metric)}"][status] += 1
            by_clause[cl][status] += 1
    return {
        "global": dict(sorted(global_counts.items())),
        "by": {k: dict(sorted(v.items())) for k, v in sorted(by.items())},
        "by_clause": {k: dict(sorted(v.items())) for k, v in sorted(by_clause.items())},
    }


def main(out: Path = Path("eval/prior.json")) -> None:
    """Записать приор в JSON файл."""
    out.write_text(json.dumps(build_prior(), ensure_ascii=False, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
