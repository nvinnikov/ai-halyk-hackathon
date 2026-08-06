"""Расчёт ковенантов по категоризованному леджеру и фактам из документов."""

import csv
import re
import sys
from collections import defaultdict

sys.path.insert(0, "solution")
from categorize import categorize
from facts import FACTS

LEDGER = "dataset/agentic-bank-public/master_ledger_2025.csv"


def norm(name: str) -> str:
    """Нормализация названия контрагента: KYC и леджер пишут их по-разному."""
    n = name.lower()
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"[\"«».,]", " ", n)
    n = re.sub(r"\b(llp|llc|jsc|ltd|inc|corp|lp|l\.l\.p|gmbh)\b", " ", n)
    return " ".join(n.split())


def load(scenario: str):
    f = FACTS[scenario]
    rows = []
    for r in csv.DictReader(open(LEDGER)):
        if r["txn_id"].split("-")[1] != scenario:
            continue
        if r["txn_id"] in f.get("exclude", []):
            continue
        amt = f.get("amount_override", {}).get(r["txn_id"])
        if amt is None:
            amt = float(r["amount"]) if r["amount"].strip() else None
        if amt is None:
            continue  # сумма не восстановлена — строка непригодна
        if r["currency"] != "USD":
            amt *= f.get("fx", {}).get(r["currency"], 1.0)
        cat = categorize(r["description"])
        for rc in f.get("reclass", []):
            if rc.get("txn") == r["txn_id"] or (
                rc.get("counterparty") and norm(rc["counterparty"]) == norm(r["counterparty"])
            ):
                cat = rc["to"]
        rows.append(
            {
                "id": r["txn_id"],
                "date": r["date"],
                "cp": r["counterparty"],
                "desc": r["description"],
                "amt": amt,
                "cat": cat,
            }
        )
    return rows, f


def totals(rows):
    """Расходы по категориям — модуль суммы отрицательных строк."""
    t = defaultdict(float)
    for r in rows:
        if r["amt"] < 0:
            t[r["cat"]] += -r["amt"]
    return t


def revenue(rows, q4_only=False):
    s = 0.0
    for r in rows:
        if r["cat"] == "REVENUE" and r["amt"] > 0:
            if q4_only and r["date"][5:7] not in ("10", "11", "12"):
                continue
            s += r["amt"]
    return s


def inflow(rows, cat):
    return sum(r["amt"] for r in rows if r["cat"] == cat and r["amt"] > 0)


def related_payments(rows, f):
    parties = [norm(p) for p in f.get("related_parties", [])]
    out = []
    for r in rows:
        if r["amt"] < 0 and any(p in norm(r["cp"]) or norm(r["cp"]) in p for p in parties):
            out.append(r)
    return out
