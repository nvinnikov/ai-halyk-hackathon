"""Легаси-формулы ковенантов — эталон для парити-теста DSL (test_templates).

Из runtime-пути ушли в задаче 16: боевой расчёт — интерпретатор DSL по
шаблонам. Каждая функция возвращает фактическое значение показателя
(положительное); направление сравнения и порог заданы отдельно в SPECS.
"""

import sys
from decimal import Decimal

sys.path.insert(0, "solution")
from engine import inflow, norm, related_payments, revenue, totals


def ebitda(rows):
    t = totals(rows)
    return revenue(rows) - t["OTHER_OPEX"]


def related_total(rows, f):
    return sum((-r["amt"] for r in related_payments(rows, f)), Decimal(0))


# --- показатели -------------------------------------------------------------

M = {}


def metric(name):
    def deco(fn):
        M[name] = fn
        return fn

    return deco


@metric("icr")  # EBITDA / Процентные расходы
def _(rows, f):
    return ebitda(rows) / totals(rows)["INTEREST"]


@metric("max_overhead_line")  # max(оплата труда, коммунальные)
def _(rows, f):
    t = totals(rows)
    return max(t["PAYROLL"], t["UTILITIES"])


@metric("related_abs")
def _(rows, f):
    return related_total(rows, f)


@metric("related_share_revenue")
def _(rows, f):
    return related_total(rows, f) / revenue(rows)


@metric("related_share_opex")
def _(rows, f):
    return related_total(rows, f) / totals(rows)["OTHER_OPEX"]


@metric("revenue")
def _(rows, f):
    return revenue(rows)


@metric("revenue_q4")
def _(rows, f):
    return revenue(rows, q4_only=True)


@metric("capex")
def _(rows, f):
    return totals(rows)["CAPEX"]


@metric("capital_intensity")  # CapEx / (OpEx + аренда)
def _(rows, f):
    t = totals(rows)
    return t["CAPEX"] / (t["OTHER_OPEX"] + t["RENT"])


@metric("sources_cover")  # (выручка + финансирование) / (OpEx + CapEx)
def _(rows, f):
    t = totals(rows)
    return (revenue(rows) + inflow(rows, "FINANCING")) / (t["OTHER_OPEX"] + t["CAPEX"])


@metric("springing_leverage")  # поступления по финансированию / EBITDA
def _(rows, f):
    return inflow(rows, "FINANCING") / ebitda(rows)


@metric("adj_ebitda_margin")
def _(rows, f):
    # Числовые факты досье приходят строками: Decimal + float — TypeError,
    # а float в денежной сумме тянет двоичный шум в actual.
    materiality = Decimal(str(f.get("addback_materiality", 0)))
    addbacks = sum(
        (a for a in (Decimal(str(x)) for x in f.get("ebitda_addbacks", [])) if a >= materiality),
        Decimal(0),
    )
    return (ebitda(rows) + addbacks) / revenue(rows)


@metric("group_capex_to_ebitda")
def _(rows, f):
    return totals(rows)["CAPEX"] / ebitda(rows)


@metric("tax_utility_to_ebitda")
def _(rows, f):
    t = totals(rows)
    return (t["TAX"] + t["UTILITIES"]) / ebitda(rows)


@metric("staff_liabilities")  # оплата труда за период + обязательство по выходным пособиям
def _(rows, f):
    return totals(rows)["PAYROLL"] + Decimal(str(f.get("severance_liability", 0)))


@metric("revenue_cover_payroll_utilities")
def _(rows, f):
    t = totals(rows)
    return revenue(rows) / (t["PAYROLL"] + t["UTILITIES"])


@metric("unrestricted_transfer_share")
def _(rows, f):
    subs = [norm(s) for s in f.get("unrestricted_subsidiaries", [])]
    moved = sum(
        (
            -r["amt"]
            for r in rows
            if r["cat"] == "CAPEX"
            and r["amt"] < 0
            and any(s in norm(r["counterparty"]) or norm(r["counterparty"]) in s for s in subs)
        ),
        Decimal(0),
    )
    return moved / totals(rows)["CAPEX"]


@metric("insurance_cover")  # страховые премии / (аренда + коммунальные)
def _(rows, f):
    t = totals(rows)
    return t["INSURANCE"] / (t["RENT"] + t["UTILITIES"])


@metric("revenue_less_max_overhead")
def _(rows, f):
    t = totals(rows)
    return revenue(rows) - max(t["PAYROLL"], t["TAX"])
