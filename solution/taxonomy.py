"""Двухуровневая таксономия категорий (5.5): листья и явные роллапы.

OTHER — корзина неразнесённого. В прикладные роллапы (OPEX_TOTAL) он не
входит: любая сумма в нём означает, что часть расхода потерялась и тихо
завышает EBITDA. Исключение — ALL, который по смыслу «все строки» и OTHER
содержит.

Отсюда agg(ALL, ...) неразнесённые строки считает, но спасает это только
числитель метрик связанных сторон. Знаменатели слепы: related_share_revenue
делит на agg(REVENUE, in), related_share_opex — на agg(OTHER_OPEX, out).
Потерянная строка выручки завышает долю, и max-ковенант по ней уходит в
ложный BREACH — знаменатель стоит статуса ровно так же, как числитель.
Поэтому алярм cell_other_alarm на related_share_* законен и списывать его
как ложный нельзя.
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


def cell_other_alarm(
    rows: list[dict], referenced: set[str], metric_filters: tuple[str, ...] = ()
) -> dict | None:
    """Потерянная строка глазами одной ячейки (5.3): что метрика не увидит.

    Слепа та категория, чьё развёртывание не содержит OTHER. Метрика,
    читающая ALL, неразнесённые строки считает — для неё алярма нет.

    Тяжесть меряется долей не от леджера заёмщика, а от того, что метрика
    вообще видит: 18 млн в OTHER при EBITDA 2.3 млн — катастрофа, при
    выручке 500 млн — шум. Порога у severity нет: алярм срабатывает при
    любой ненулевой сумме, severity задаёт лишь порядок разбора.

    Охват — по категории, но НЕ по фильтрам Agg-узлов: строки берутся все,
    хотя метрика с quarter(4) или desc_contains видит лишь часть. Отсюда
    два перекоса на таких ячейках — ложное срабатывание (потерянная строка
    Q1 поднимает алярм на квартальной метрике) и мягкая severity (в
    знаменателе годовая сумма вместо квартальной). Учесть фильтры точно
    значит считать алярм поузлово с предикатом interp, то есть завести в
    таксономию знание об AST; вместо этого metric_filters перечисляет
    неучтённые фильтры в трейсе, чтобы severity не читали как точную.
    """
    if not referenced:
        return None
    blind = []
    for name in sorted(referenced):
        try:
            leaves = expand(name)
        except KeyError:
            # Незнакомая категория (например, пришедшая от LLM) считается
            # слепой: молчать здесь опаснее, чем лишний раз предупредить.
            blind.append(name)
            continue
        if "OTHER" not in leaves:
            blind.append(name)
    if not blind:
        return None

    ordered = sorted(rows, key=lambda r: r["txn_id"])
    other_rows = [r for r in ordered if r["cat"] == "OTHER"]
    other_sum = sum((abs(r["amt"]) for r in other_rows), Decimal(0))
    if other_sum == 0:
        return None

    blind_leaves: set[str] = set()
    for name in blind:
        try:
            blind_leaves |= set(expand(name))
        except KeyError:
            continue
    inputs_sum = sum((abs(r["amt"]) for r in ordered if r["cat"] in blind_leaves), Decimal(0))
    severity = str((other_sum / inputs_sum).quantize(Decimal("0.000001"))) if inputs_sum else None
    out = {
        "blind": blind,
        "other_sum": str(other_sum),
        "inputs_sum": str(inputs_sum),
        "severity": severity,
        "txn_ids": [r["txn_id"] for r in other_rows],
    }
    if metric_filters:
        # Метрика видит часть строк, алярм посчитан по всем: severity — верхняя
        # оценка охвата, а само срабатывание может относиться к строке, которую
        # метрика не читает. Разбирать такую ячейку — после нефильтрованных.
        out["coverage"] = "unfiltered"
        out["ignored_filters"] = sorted(set(metric_filters))
    return out
