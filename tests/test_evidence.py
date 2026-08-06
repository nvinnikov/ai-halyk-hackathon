"""Контрфактуал — откат решения, а не удаление операции: для исключений
разница принципиальная (повторное удаление исключённой строки — пустая операция)."""

from decimal import Decimal
from pathlib import Path

from dsl import parse
from evidence import candidates, find

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")


def row(txn, cat, amt, cp="X", desc="d", date="2025-06-01"):
    return {
        "txn_id": txn,
        "cat": cat,
        "amt": Decimal(amt),
        "counterparty": cp,
        "description": desc,
        "date": date,
        "account_id": "ACC-1",
        "currency": "USD",
    }


def spec(metric, direction, limit, trigger=None):
    return {
        "metric_ast": parse(metric),
        "direction": direction,
        "limit": Decimal(limit),
        "trigger_ast": trigger,
    }


def test_exclusion_rollback_flips():
    # исключённая строка выручки: откат возвращает её и чинит BREACH по min
    raw = [row("T-1", "REVENUE", "100"), row("T-2", "REVENUE", "1000", date="2026-01-15")]
    facts = {"exclude": ["T-2"], "exclude_quotes": {"T-2": "переход рисков в 2026"}}
    s = spec("agg(REVENUE, in)", "min", "500")
    ev, trace = find(raw, facts, s, "BREACH")
    assert ev == "T-2"
    assert any(t["decision_type"] == "exclusion" for t in trace)


def test_leave_one_out_contributor_is_not_candidate():
    # 550k + 50k при пороге 500k: без документального решения ожидается null
    raw = [row("T-1", "CAPEX", "-550000"), row("T-2", "CAPEX", "-50000")]
    s = spec("agg(CAPEX, out)", "max", "500000")
    ev, _ = find(raw, {}, s, "BREACH")
    assert ev is None
    assert candidates(raw, {}, s) == []


def test_inclusion_rollback():
    # платёж ограничен ровно потому, что KYC признал контрагента связанным
    raw = [
        row("T-1", "OTHER_OPEX", "-600", cp="Ertis Capital LLP"),
        row("T-2", "RENT", "-100", cp="Somebody Else"),
    ]
    facts = {
        "related_parties": ["Ertis Capital LLP"],
        "related_quotes": {"Ertis Capital LLP": "KYC: связанная сторона"},
    }
    s = spec("agg(ALL, out, counterparty_in(related_parties))", "max", "500")
    ev, _ = find(raw, facts, s, "BREACH")
    assert ev == "T-1"


def test_two_flippers_mean_null():
    raw = [
        row("T-1", "OTHER_OPEX", "-600", cp="Ertis Capital LLP"),
        row("T-2", "OTHER_OPEX", "-600", cp="Ertis Capital LLP"),
    ]
    facts = {"related_parties": ["Ertis Capital LLP"]}
    # порог 1000: откат любого из двух переворачивает — улика не единственна
    s = spec("agg(ALL, out, counterparty_in(related_parties))", "max", "1000")
    ev, _ = find(raw, facts, s, "BREACH")
    assert ev is None


def test_amount_fix_rollback():
    raw = [row("T-1", "TAX", "-100")]
    facts = {"amount_override": {"T-1": "-600"}, "override_quotes": {"T-1": "записка казначейства"}}
    s = spec("agg(TAX, out)", "max", "500")
    ev, _ = find(raw, facts, s, "BREACH")
    assert ev == "T-1"


def test_doc_only_metric_yields_null():
    s = spec("ratio(doc(a), doc(b))", "max", "1")
    ev, _ = find([], {"doc_facts": {"a": "5", "b": "1"}}, s, "BREACH")
    assert ev is None


def test_compliant_yields_null():
    ev, _ = find([row("T-1", "TAX", "-1")], {}, spec("agg(TAX, out)", "max", "500"), "COMPLIANT")
    assert ev is None


def test_public_key_all_nine_found():
    """Интеграция: все 9 непустых улик публичного ключа достаются алгоритмом."""
    import json

    import solve

    gt = json.loads(Path("dataset/agentic-bank-public/ground_truth.json").read_text())["scenarios"]
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    missed = [
        (sc, cl)
        for sc in gt
        for cl, key in gt[sc]["covenants"].items()
        if key["evidence_txn_id"] is not None and answers[sc][cl]["evidence_txn_id"] != key["evidence_txn_id"]
    ]
    assert missed == [], f"пропущенные улики: {missed}"
