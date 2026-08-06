"""Формулы ковенантов по заёмщикам.

Каждая функция возвращает фактическое значение показателя (положительное).
Направление сравнения и порог заданы отдельно в SPECS.
"""

import sys

sys.path.insert(0, "solution")
from engine import inflow, norm, related_payments, revenue, totals


def ebitda(rows):
    t = totals(rows)
    return revenue(rows) - t["OPEX"]


def related_total(rows, f):
    return sum(-r["amt"] for r in related_payments(rows, f))


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
    return related_total(rows, f) / totals(rows)["OPEX"]


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
    return t["CAPEX"] / (t["OPEX"] + t["RENT"])


@metric("sources_cover")  # (выручка + финансирование) / (OpEx + CapEx)
def _(rows, f):
    t = totals(rows)
    return (revenue(rows) + inflow(rows, "FINANCING")) / (t["OPEX"] + t["CAPEX"])


@metric("springing_leverage")  # поступления по финансированию / EBITDA
def _(rows, f):
    return inflow(rows, "FINANCING") / ebitda(rows)


@metric("adj_ebitda_margin")
def _(rows, f):
    addbacks = sum(a for a in f.get("ebitda_addbacks", []) if a >= f.get("addback_materiality", 0))
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
    return totals(rows)["PAYROLL"] + f.get("severance_liability", 0.0)


@metric("revenue_cover_payroll_utilities")
def _(rows, f):
    t = totals(rows)
    return revenue(rows) / (t["PAYROLL"] + t["UTILITIES"])


@metric("unrestricted_transfer_share")
def _(rows, f):
    subs = [norm(s) for s in f.get("unrestricted_subsidiaries", [])]
    moved = sum(
        -r["amt"]
        for r in rows
        if r["cat"] == "CAPEX"
        and r["amt"] < 0
        and "subsidiary" in r["desc"].lower()
        and any(s in norm(r["cp"]) or norm(r["cp"]) in s for s in subs)
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


# --- спецификации ячеек -----------------------------------------------------
# (метрика, направление, порог)  direction: "max" — не превышать, "min" — не ниже
SPECS = {
    "B1": {
        "6.1": ("icr", "min", 2.00),
        "6.2": ("max_overhead_line", "max", 1_500_000),
        "6.3": ("related_abs", "max", 500_000),
    },
    "B4": {
        "6.1": ("revenue_q4", "min", 3_500_000),
        "6.2": ("capex", "max", 2_000_000),
        "6.3": ("related_abs", "max", 500_000),
    },
    "P1": {
        "6.1": ("capital_intensity", "max", 0.42),
        "6.2": ("revenue", "min", 7_100_000),
        "6.3": ("related_abs", "max", 450_000),
    },
    "P2": {
        "6.1": ("sources_cover", "min", 1.20),
        "6.2": ("capex", "max", 3_000_000),
        "6.3": ("related_share_revenue", "max", 0.03),
    },
    "P3": {
        "6.1": ("springing_leverage", "max", 1.70, {"trigger_financing": 4_000_000}),
        "6.2": ("revenue", "min", 6_500_000),
        "6.3": ("related_abs", "max", 400_000),
    },
    "P4": {
        "6.1": ("adj_ebitda_margin", "min", 0.28),
        "6.2": ("capex", "max", 1_800_000),
        "6.3": ("related_share_revenue", "max", 0.04),
    },
    "P5": {
        "6.1": ("group_capex_to_ebitda", "max", 9.00),
        "6.2": ("revenue", "min", 7_500_000),
        "6.3": ("related_abs", "max", 260_000),
    },
    "P6": {
        "6.1": ("related_share_opex", "max", 0.08),
        "6.2": ("revenue_cover_payroll_utilities", "min", 3.00),
        "6.3": ("capex", "max", 1_600_000),
    },
    "P7": {
        "6.1": ("tax_utility_to_ebitda", "max", 0.30),
        "6.2": ("revenue", "min", 8_700_000),
        "6.3": ("related_abs", "max", 275_000),
    },
    "P8": {
        "6.1": ("staff_liabilities", "max", 4_000_000),
        "6.2": ("capex", "max", 2_100_000),
        "6.3": ("related_share_revenue", "max", 0.04),
    },
    "P9": {
        "6.1": ("unrestricted_transfer_share", "max", 0.15),
        "6.2": ("revenue", "min", 6_900_000),
        "6.3": ("related_abs", "max", 225_000),
    },
    "P10": {
        "6.1": ("insurance_cover", "min", 0.20),
        "6.2": ("revenue_less_max_overhead", "min", 5_000_000),
        "6.3": ("related_share_revenue", "max", 0.05),
    },
}


# --- строки, формирующие ограничиваемую величину -----------------------------
# Нужны для поиска улики: если ограничиваемый набор состоит ровно из одной
# операции, именно она определяет вердикт.
def _cat(cat):
    return lambda rows, f: [r for r in rows if r["cat"] == cat and r["amt"] < 0]


DRIVERS = {
    "related_abs": lambda rows, f: related_payments(rows, f),
    "related_share_revenue": lambda rows, f: related_payments(rows, f),
    "related_share_opex": lambda rows, f: related_payments(rows, f),
    "capex": _cat("CAPEX"),
    "unrestricted_transfer_share": lambda rows, f: [
        r
        for r in rows
        if r["cat"] == "CAPEX"
        and r["amt"] < 0
        and "subsidiary" in r["desc"].lower()
        and any(
            norm(s) in norm(r["cp"]) or norm(r["cp"]) in norm(s)
            for s in f.get("unrestricted_subsidiaries", [])
        )
    ],
}
