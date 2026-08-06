"""Маршрутизация документов по заёмщикам и извлечение значимых разделов.

Детерминированный слой: определяет тип документа, отбрасывает недействующие
редакции и черновики, привязывает документ к account_id.
"""

import json
import re

STALE_MARKERS = (
    "НЕДЕЙСТВУЮЩАЯ РЕДАКЦИЯ",
    "Заменена и изложена",
    "ПРОЕКТ — ПРОМЕЖУТОЧНАЯ",
    "НЕ ЯВЛЯЕТСЯ ОКОНЧАТЕЛЬНОЙ",
    "заменяется отчётом",
    "полностью заменяется",
)


def is_stale(text: str) -> bool:
    return any(m in text for m in STALE_MARKERS)


def doc_type(text: str) -> str:
    if "ИСПОЛНИТЕЛЬНЫЙ ЭКЗЕМПЛЯР" in text and "ДОГОВОР БАНКОВСКОГО ЗАЙМА" in text:
        return "agreement"
    if "Отчёт о выполнении согласованных процедур" in text:
        return "agreed_procedures"
    if "Примечания к финансовой отчётности" in text:
        return "audit_notes"
    if "Досье «Знай своего клиента»" in text or "KYC" in text[:800]:
        return "kyc"
    if "казначейств" in text.lower() and "Служебная записка" in text:
        return "treasury"
    return "noise"


def account_of(text: str) -> str | None:
    m = re.search(r"ACC-\d+", text)
    return m.group() if m else None


def build(cache_path="solution/docs_text.json"):
    docs = json.load(open(cache_path))
    dossiers = {}
    for name, text in docs.items():
        kind = doc_type(text)
        if kind == "noise":
            continue
        acc = account_of(text)
        if acc is None:
            continue
        rec = {"file": name, "type": kind, "stale": is_stale(text), "text": text}
        dossiers.setdefault(acc, []).append(rec)
    return dossiers


if __name__ == "__main__":
    d = build()
    for acc in sorted(d):
        live = [r for r in d[acc] if not r["stale"]]
        dead = [r for r in d[acc] if r["stale"]]
        print(
            acc,
            "|",
            ", ".join(f"{r['type']}:{r['file'][:8]}" for r in live),
            "|| отброшено:",
            ", ".join(f"{r['type']}:{r['file'][:8]}" for r in dead),
        )
