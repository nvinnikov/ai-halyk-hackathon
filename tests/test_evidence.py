"""Контрфактуал — откат решения, а не удаление операции: для исключений
разница принципиальная (повторное удаление исключённой строки — пустая операция)."""

from decimal import Decimal
from pathlib import Path

import evidence
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


def test_no_document_decision_still_yields_ledger_evidence():
    # 550k + 50k при пороге 500k: документальных решений нет (candidates
    # пуст), но снятие T-1 в одиночку переворачивает вердикт (50k <= 500k) —
    # новая политика улики не отдаёт null там, где леджер даёт переворот.
    raw = [row("T-1", "CAPEX", "-550000"), row("T-2", "CAPEX", "-50000")]
    s = spec("agg(CAPEX, out)", "max", "500000")
    assert candidates(raw, {}, s) == []
    ev, _ = find(raw, {}, s, "BREACH")
    assert ev == "T-1"


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


def test_two_equal_flippers_pick_lexicographically_first_txn():
    raw = [
        row("T-1", "OTHER_OPEX", "-600", cp="Ertis Capital LLP"),
        row("T-2", "OTHER_OPEX", "-600", cp="Ertis Capital LLP"),
    ]
    facts = {"related_parties": ["Ertis Capital LLP"]}
    # порог 1000: откат любого из двух переворачивает, суммы равны — раньше
    # это давало null, новая политика решает тай-брейк по txn_id.
    s = spec("agg(ALL, out, counterparty_in(related_parties))", "max", "1000")
    ev, _ = find(raw, facts, s, "BREACH")
    assert ev == "T-1"


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


def _cellspec(metric: str, direction: str, limit: str) -> dict:
    return {
        "metric_ast": parse(metric),
        "metric_text": metric,
        "trigger_ast": None,
        "direction": direction,
        "limit": Decimal(limit),
    }


def _flip_rows():
    return [
        row("T-1", "CAPEX", "-900", cp="A LLP", date="2025-02-01"),
        row("T-2", "CAPEX", "-30", cp="B LLP", date="2025-03-01"),
        row("T-3", "RENT", "-5000", cp="C LLP", date="2025-04-01"),
    ]


def test_single_flipping_ledger_row_becomes_evidence():
    # Сумма CAPEX 930, порог 800 → BREACH. Снятие T-1 даёт 30 (< 800) —
    # переворот; снятие T-2 даёт 900 (> 800) — не переворот. Переворачивающий
    # ровно один.
    spec = _cellspec("agg(CAPEX, out)", "max", "800")
    txn, trace = evidence.find(_flip_rows(), {}, spec, "BREACH")
    assert txn == "T-1"
    assert any(t["flipped"] for t in trace)


def test_several_flippers_pick_largest_not_null():
    # Порог 920: снятие T-1 даёт 30 (< 920) — переворот; снятие T-2 даёт 900
    # (< 920) — тоже переворот. Переворачивающих два: раньше это давало null.
    spec = _cellspec("agg(CAPEX, out)", "max", "920")
    txn, _ = evidence.find(_flip_rows(), {}, spec, "BREACH")
    assert txn == "T-1"  # крупнейшая из переворачивающих, а не null


def test_no_flipper_falls_back_to_largest_read_row():
    # Порог 10: не спасает снятие ни одной строки — раньше это давало null.
    spec = _cellspec("agg(CAPEX, out)", "max", "10")
    txn, _ = evidence.find(_flip_rows(), {}, spec, "BREACH")
    assert txn == "T-1"


def test_rent_row_never_becomes_evidence_for_capex_metric():
    spec = _cellspec("agg(CAPEX, out)", "max", "10")
    txn, _ = evidence.find(_flip_rows(), {}, spec, "BREACH")
    assert txn != "T-3"


def test_compliant_cell_still_has_no_evidence():
    spec = _cellspec("agg(CAPEX, out)", "max", "100000")
    txn, trace = evidence.find(_flip_rows(), {}, spec, "COMPLIANT")
    assert txn is None
    assert trace == []


def test_doc_only_metric_has_no_evidence():
    spec = _cellspec("doc(group_capex)", "max", "10")
    txn, _ = evidence.find(_flip_rows(), {"doc_facts": {"group_capex": "500"}}, spec, "BREACH")
    assert txn is None


def test_reading_rows_respects_sign_and_filters():
    rows = _flip_rows() + [
        row("T-4", "CAPEX", "70", cp="D LLP", date="2025-05-01"),
    ]
    got = evidence.reading_rows(parse("agg(CAPEX, out)"), rows, {})
    assert [r["txn_id"] for r in got] == ["T-1", "T-2"]


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
