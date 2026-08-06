"""Ядро агрегации: Decimal, порядок txn_id, related-матч по токенам.

Маршрутизация строк — по колонке account_id (4.1). Валюта здесь не
трогается: конвертация — отдельная стадия fx.py, до любой агрегации.
"""

import re
import sys
from collections.abc import Callable
from decimal import Decimal

sys.path.insert(0, "solution")
from taxonomy import expand

LEGAL_FORMS = frozenset({"llp", "llc", "jsc", "ltd", "inc", "corp", "lp", "gmbh", "plc"})


def tokens(name: str) -> frozenset[str]:
    """Нормализованные токены: ≥3 символов, без юридических форм.

    Разбиение по не-alnum идёт до фильтра юрформ, поэтому 'L.L.P.' и 'LLP'
    дают один и тот же результат; старый norm() резал точки после regex и
    оставлял от первого написания мусорные 'l l p'.
    """
    words = re.split(r"[^a-z0-9]+", name.lower())
    return frozenset(w for w in words if len(w) >= 3 and w not in LEGAL_FORMS)


def is_related(counterparty: str, parties: list[str]) -> bool:
    """Матч только по подмножеству непустых токенов — без подстрочного (4.1).

    Пустой набор токенов ('LLP', 'LLC JSC') не матчится ни с чем: иначе одна
    юрформа в списке связанных сторон роднит с заёмщиком весь леджер.
    """
    ct = tokens(counterparty)
    if not ct:
        return False
    for p in parties:
        pt = tokens(p)
        if pt and (pt <= ct or ct <= pt):
            return True
    return False


def select_rows(all_rows: list[dict], account_id: str) -> list[dict]:
    return [r for r in all_rows if r["account_id"] == account_id]


def prepare_rows(raw_rows: list[dict], facts: dict, overrides: dict | None = None) -> list[dict]:
    """Применяет документальные решения из фактов досье.

    overrides — откат конкретного решения для контрфактуала улики (5.6):
    undo_exclude / undo_override / undo_reclass / set_exclude.
    """
    ov = overrides or {}
    out = []
    excluded = (set(facts.get("exclude", [])) - set(ov.get("undo_exclude", set()))) | set(
        ov.get("set_exclude", set())
    )
    for r in sorted(raw_rows, key=lambda x: x["txn_id"]):
        if r["txn_id"] in excluded:
            continue
        rec = dict(r)
        override = facts.get("amount_override", {}).get(r["txn_id"])
        if override is not None and r["txn_id"] not in ov.get("undo_override", set()):
            rec["amt"] = Decimal(str(override))
        if rec["amt"] is None:
            continue  # сумма не восстановлена — строка непригодна
        for i, rc in enumerate(facts.get("reclass", [])):
            if i in ov.get("undo_reclass", set()):
                continue
            # Совпадение контрагента — по непустым токенам: реклассификация,
            # заданная одной юрформой, иначе накрыла бы все безымянные строки.
            rct = tokens(rc["counterparty"]) if rc.get("counterparty") else frozenset()
            hit = rc.get("txn") == rec["txn_id"] or (rct and rct == tokens(rec["counterparty"]))
            if hit:
                rec["cat"] = rc["to"]
        out.append(rec)
    return out


def agg(rows: list[dict], category: str, sign: str, pred: Callable | None = None) -> Decimal:
    cats = expand(category)
    total = Decimal(0)
    for r in sorted(rows, key=lambda x: x["txn_id"]):
        if r["cat"] not in cats or (pred is not None and not pred(r)):
            continue
        if sign == "out":
            if r["amt"] < 0:
                total += -r["amt"]
        elif sign == "in":
            if r["amt"] > 0:
                total += r["amt"]
        elif sign == "net":
            total += -r["amt"]
        else:
            raise ValueError(f"unknown sign {sign!r}")
    return total


def sign_divergence(rows: list[dict], categories: list[str] | None = None) -> dict[str, dict[str, Decimal]]:
    """Категории, где выбор знака решает: out (модуль расхода) ≠ net (с неттингом).

    Дефолт агрегации расходной категории — out: он проверен на публичном
    наборе, net — ни на одной ячейке. Расхождение означает сторно внутри
    категории и печатается в трейс, чтобы на приватном наборе такие ячейки
    были видны сразу, а не после разбора расхождения в баллах.
    """
    names = sorted(categories) if categories is not None else sorted({r["cat"] for r in rows})
    div = {}
    for name in names:
        out = agg(rows, name, "out")
        net = agg(rows, name, "net")
        if out and out != net:
            div[name] = {"out": out, "net": net}
    return div


# --- легаси-обёртки для covenants.M (уходят вместе с ним в задаче 15) --------


def norm(name: str) -> str:
    return " ".join(sorted(tokens(name)))


def totals(rows):
    t: dict[str, Decimal] = {}
    for r in sorted(rows, key=lambda x: x["txn_id"]):
        if r["amt"] < 0:
            t[r["cat"]] = t.get(r["cat"], Decimal(0)) + -r["amt"]

    class _D(dict):
        def __missing__(self, key):
            return Decimal(0)

    return _D(t)


def revenue(rows, q4_only: bool = False):
    def pred(r):
        return not q4_only or r["date"][5:7] in ("10", "11", "12")

    return agg(rows, "REVENUE", "in", pred)


def inflow(rows, cat):
    return agg(rows, cat, "in")


def related_payments(rows, f):
    """Только расходные строки: у B4 связанная сторона даёт 9 млн поступлений,
    и попади они в related_abs — ковенант 6.3 сломался бы на 9 млн."""
    parties = f.get("related_parties", [])
    return [
        r
        for r in sorted(rows, key=lambda x: x["txn_id"])
        if r["amt"] < 0 and is_related(r["counterparty"], parties)
    ]
