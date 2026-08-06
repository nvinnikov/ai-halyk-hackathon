"""Эмпирический приор статусов из публичного ключа (5.7).

Условиться по номеру пункта нельзя (греп-гейт + конфаундинг с типом метрики);
семья метрики забирает ту же информацию законным способом.
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
    """Построить приор статусов из ground truth по метрикам и направлениям."""
    gt = json.loads(GT.read_text())["scenarios"]
    global_counts: dict[str, int] = defaultdict(int)
    by: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for sc in sorted(gt):
        for cl in sorted(gt[sc]["covenants"]):
            status = gt[sc]["covenants"][cl]["status"]
            metric, direction = SPECS[sc][cl][0], SPECS[sc][cl][1]
            global_counts[status] += 1
            by[f"{direction}|{metric_family(metric)}"][status] += 1
    return {
        "global": dict(sorted(global_counts.items())),
        "by": {k: dict(sorted(v.items())) for k, v in sorted(by.items())},
    }


def main(out: Path = Path("eval/prior.json")) -> None:
    """Записать приор в JSON файл."""
    out.write_text(json.dumps(build_prior(), ensure_ascii=False, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
