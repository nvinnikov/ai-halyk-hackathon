"""Двухуровневая таксономия категорий (5.5): листья и явные роллапы.

OTHER — корзина неразнесённого, не входит ни в один роллап: любая сумма
в ней означает, что часть расхода потерялась и тихо завышает EBITDA.
"""

from decimal import Decimal

LEAVES = frozenset(
    {
        "REVENUE",
        "PAYROLL",
        "UTILITIES",
        "RENT",
        "TAX",
        "INTEREST",
        "CAPEX",
        "INSURANCE",
        "FINANCING",
        "MARKETING",
        "TELECOM",
        "CONSULTING",
        "OTHER_OPEX",
        "OTHER",
    }
)

ROLLUPS: dict[str, frozenset[str]] = {
    "OPEX_TOTAL": frozenset(
        {
            "PAYROLL",
            "UTILITIES",
            "RENT",
            "INSURANCE",
            "OTHER_OPEX",
            "MARKETING",
            "TELECOM",
            "CONSULTING",
        }
    ),
    "ALL": LEAVES,
}

OTHER_SHARE_THRESHOLD = Decimal("0.005")


def is_category(name: str) -> bool:
    return name in LEAVES or name in ROLLUPS


def expand(name: str) -> frozenset[str]:
    if name in LEAVES:
        return frozenset({name})
    if name in ROLLUPS:
        return ROLLUPS[name]
    raise KeyError(name)


def coverage_report(rows: list[dict], referenced: set[str] | None = None) -> dict:
    """Покрытие считается по доле СУММЫ, а не по числу строк: одна потерянная
    строка на 90 млн опаснее сотни мелких, и счётчик строк её не увидит."""
    by_cat: dict[str, Decimal] = {}
    total = Decimal(0)
    for r in sorted(rows, key=lambda x: x["txn_id"]):
        a = abs(r["amt"])
        by_cat[r["cat"]] = by_cat.get(r["cat"], Decimal(0)) + a
        total += a
    other_share = (by_cat.get("OTHER", Decimal(0)) / total) if total else Decimal(0)
    alarm = "none"
    if other_share > OTHER_SHARE_THRESHOLD:
        alarm = "warn"
        # Роллап разворачивается в листья: ковенант, читающий OPEX_TOTAL,
        # задет потерей в OTHER так же, как читающий OTHER_OPEX напрямую.
        touched = set().union(*(expand(c) for c in (referenced or set()))) | (referenced or set())
        if touched & {"OPEX_TOTAL", "OTHER"} or "OTHER_OPEX" in touched:
            alarm = "critical"
    return {
        "by_cat_sum": {k: str(v) for k, v in sorted(by_cat.items())},
        "other_share": float(other_share),
        "alarm": alarm,
    }
