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


def test_smaller_flipping_row_beats_larger_non_flipping_row():
    # Разделяющий тест: для sub(REVENUE_in, OPEX_out) снятие любой строки
    # меняет знаменатель по-разному, поэтому "крупнейшая читаемая" и
    # "крупнейшая переворачивающая" здесь не совпадают — подделка без учёта
    # flipped выбрала бы T-1 (крупнейшая), а не T-2 (переворачивающая).
    # 1000 − 300 = 700 < 800 → BREACH. Без T-1 (REVENUE, крупнейшая читаемая):
    # 0 − 300 = −300 < 800 → BREACH, не переворачивает. Без T-2 (OTHER_OPEX,
    # меньшая читаемая): 1000 − 0 = 1000, не < 800 → COMPLIANT, переворачивает.
    raw = [
        row("T-1", "REVENUE", "1000", date="2025-02-01"),
        row("T-2", "OTHER_OPEX", "-300", date="2025-03-01"),
    ]
    s = spec("sub(agg(REVENUE, in), agg(OTHER_OPEX, out))", "min", "800")
    ev, trace = find(raw, {}, s, "BREACH")
    flips = {t["txn"]: t["flipped"] for t in trace}
    assert flips == {"T-1": False, "T-2": True}
    assert ev == "T-2"


def test_document_decision_outranks_larger_ledger_row():
    # Разделяющий тест на приоритет ранга типа решения над суммой: T-1 CAPEX
    # −900 без документального решения, T-2 в леджере −10, но документ
    # (amount_fix) поднимает её до −50. Порог 920, max: по умолчанию
    # 900+50=950 > 920 → BREACH. Без T-1: 50 > 920? нет → COMPLIANT,
    # переворот. Откат поправки (T-2 возвращается к −10): 900+10=910 > 920?
    # нет → COMPLIANT, тоже переворот — обе строки переворачивают, но
    # документальный кандидат (T-2, меньшая сумма) обязан победить крупную
    # T-1 по рангу типа решения, а не по модулю суммы.
    raw = [
        row("T-1", "CAPEX", "-900", date="2025-02-01"),
        row("T-2", "CAPEX", "-10", date="2025-03-01"),
    ]
    facts = {"amount_override": {"T-2": "-50"}, "override_quotes": {"T-2": "записка казначейства"}}
    s = spec("agg(CAPEX, out)", "max", "920")
    ev, trace = find(raw, facts, s, "BREACH")
    flippers = {t["txn"] for t in trace if t["flipped"]}
    assert flippers == {"T-1", "T-2"}  # обе строки переворачивают
    assert ev == "T-2"  # но побеждает документальное решение, не крупная сумма


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


def test_inclusion_rollback_keeps_the_denominator_whole():
    """Откат признания связанности снимает строку только из числителя.

    Финальное ревью ветки, §4: глобальное отсечение давало (N−r)/(D−r) вместо
    (N−r)/D, а второе всегда больше — на max-ковенанте документальный кандидат
    переставал переворачивать вердикт, и улику забирала догадка по леджеру, то
    есть проигрыш происходил на первой, спекой определённой ступени. Здесь
    ровно этот случай: 100/400 = 0.25 переворачивает порог, 100/300 = 0.33 —
    нет."""
    raw = [
        row("T-1", "OTHER_OPEX", "-100", cp="Ertis Capital LLP"),
        row("T-2", "OTHER_OPEX", "-100", cp="Ertis Capital LLP"),
        row("T-3", "OTHER_OPEX", "-200"),
    ]
    facts = {
        "related_parties": ["Ertis Capital LLP"],
        "related_quotes": {"Ertis Capital LLP": "доля 51%"},
    }
    s = spec(
        "ratio(agg(ALL, out, counterparty_in(related_parties)), agg(OTHER_OPEX, out))",
        "max",
        "0.30",
    )
    ev, trace = find(raw, facts, s, "BREACH")
    assert ev == "T-1"
    assert [t["decision_type"] for t in trace if t["txn"] == "T-1" and t["flipped"]] == ["inclusion"]
