"""norm('LLP') == '' делало связанными всех; sign=net чинит потерю сторно."""

from decimal import Decimal

from engine import agg, is_related, prepare_rows, related_payments, select_rows, sign_divergence, tokens


def row(txn, cat, amt, cp="X", desc="d", acc="ACC-1", date="2025-06-01", cur="USD"):
    return {
        "txn_id": txn,
        "cat": cat,
        "amt": Decimal(amt) if amt is not None else None,
        "counterparty": cp,
        "description": desc,
        "account_id": acc,
        "date": date,
        "currency": cur,
    }


def test_tokens_drop_legal_forms_and_short():
    assert tokens("Ertis Capital, LLP") == frozenset({"ertis", "capital"})
    assert tokens("LLP") == frozenset()


def test_tokens_survive_dotted_legal_form():
    """Старый norm() резал точки раньше юрформ: 'l l p' оставался токенами и
    'L.L.P.' не совпадало с 'LLP'. Спека 4.1 запрещает подстрочный матч,
    которым это раньше маскировалось."""
    assert tokens("Aktau Holdings L.L.P.") == tokens("Aktau Holdings LLP")
    assert tokens("Aktau Holdings L.L.P.") == frozenset({"aktau", "holdings"})


def test_is_related_two_sided_subset_nonempty():
    assert is_related("Ertis Capital LLP", ["Ertis Capital, LLP"])
    assert is_related('"Ertis Capital" Group LLP', ["Ertis Capital"])
    assert is_related("Aktau Holdings L.L.P.", ["Aktau Holdings LLP"])
    assert not is_related("Anything Inc", ["LLP"])  # пустые токены — не матч
    assert not is_related("Ertis Capital LLP", [])


def test_is_related_ignores_legal_form_only_key():
    """130 ключей старого norm() схлопывались в 'llc jsc' и роднили всех со всеми."""
    assert not is_related("Ashford Office Supplies LLC", ["LLC JSC"])


def test_is_related_has_no_substring_match():
    """Подстрочный матч роднил 'Capital' с 'Capital Partners' и наоборот —
    в is_related остаётся только подмножество непустых токенов."""
    assert not is_related("Aral Capital Partners LLP", ["Aral Grain Terminal LLP"])


def test_select_rows_by_account_column():
    rows = [row("T-1", "REVENUE", "1"), row("T-2", "REVENUE", "1", acc="ACC-2")]
    assert [r["txn_id"] for r in select_rows(rows, "ACC-1")] == ["T-1"]


def test_agg_signs():
    rows = [
        row("T-1", "PAYROLL", "-100"),
        row("T-2", "PAYROLL", "30"),  # возврат аванса
        row("T-3", "RENT", "-50"),
    ]
    assert agg(rows, "PAYROLL", "out") == Decimal("100")
    assert agg(rows, "PAYROLL", "in") == Decimal("30")
    assert agg(rows, "PAYROLL", "net") == Decimal("70")
    assert agg(rows, "OPEX_TOTAL", "out") == Decimal("150")


def test_agg_sums_in_txn_order():
    rows = [row("T-2", "TAX", "-1"), row("T-3", "TAX", "-2"), row("T-1", "TAX", "-4")]
    seen = []

    def spy(r):
        seen.append(r["txn_id"])
        return True

    agg(rows, "TAX", "out", spy)
    assert seen == sorted(seen), f"обход не по txn_id: {seen}"


def test_sign_divergence_finds_reversals_only():
    rows = [row("T-1", "MARKETING", "-100"), row("T-2", "MARKETING", "40"), row("T-3", "RENT", "-50")]
    div = sign_divergence(rows)
    assert set(div) == {"MARKETING"}
    assert div["MARKETING"] == {"out": Decimal("100"), "net": Decimal("60")}


def test_related_payments_ignore_incoming():
    """У B4 связанная сторона даёт 9 млн поступлений: возьми их related-логика —
    related_abs подскочит на 9 млн и ковенант 6.3 сломается."""
    rows = [
        row("T-1", "REVENUE", "9000000", cp="Shymkent Fuel Distributors LLP"),
        row("T-2", "OTHER_OPEX", "-1000", cp="Shymkent Fuel Distributors LLP"),
    ]
    f = {"related_parties": ["Shymkent Fuel Distributors LLP"]}
    assert [r["txn_id"] for r in related_payments(rows, f)] == ["T-2"]


def test_prepare_rows_facts_and_overrides():
    raw = [
        row("T-1", "CONSULTING", "-10", cp="Tien Shan Advisory Bureau"),
        row("T-2", "CAPEX", "-99"),
        row("T-3", "TAX", "-5"),
    ]
    facts = {
        "reclass": [{"txn": None, "counterparty": "Tien Shan Advisory Bureau", "to": "OTHER_OPEX"}],
        "exclude": ["T-2"],
        "amount_override": {"T-3": "-7"},
    }
    rows = prepare_rows(raw, facts)
    by = {r["txn_id"]: r for r in rows}
    assert by["T-1"]["cat"] == "OTHER_OPEX"
    assert "T-2" not in by
    assert by["T-3"]["amt"] == Decimal("-7")

    undone = prepare_rows(raw, facts, overrides={"undo_exclude": {"T-2"}})
    assert "T-2" in {r["txn_id"] for r in undone}

    restored = prepare_rows(raw, facts, overrides={"undo_override": {"T-3"}})
    assert {r["txn_id"]: r for r in restored}["T-3"]["amt"] == Decimal("-5")

    kept = prepare_rows(raw, facts, overrides={"undo_reclass": {0}})
    assert {r["txn_id"]: r for r in kept}["T-1"]["cat"] == "CONSULTING"

    dropped = prepare_rows(raw, facts, overrides={"set_exclude": {"T-3"}})
    assert "T-3" not in {r["txn_id"] for r in dropped}


def test_prepare_rows_reclass_needs_nonempty_tokens():
    """Реклассификация, заданная одной юрформой, не должна накрыть весь леджер."""
    raw = [row("T-1", "CAPEX", "-10", cp="LLP")]
    facts = {"reclass": [{"txn": None, "counterparty": "LLC", "to": "RENT"}]}
    assert prepare_rows(raw, facts)[0]["cat"] == "CAPEX"


def test_metric_categories_are_known_to_taxonomy():
    """Диагностика знака кормит эти имена в agg → expand: опечатка в списке
    уронила бы ячейку в фолбэк, а не подсветила расхождение."""
    from covenants import METRIC_CATEGORIES, M

    assert set(METRIC_CATEGORIES) <= set(M), sorted(set(METRIC_CATEGORIES) - set(M))
    for cats in METRIC_CATEGORIES.values():
        for cat in cats:
            agg([], cat, "out")  # expand кинет KeyError на неизвестной категории


def test_prepare_rows_does_not_mutate_input():
    raw = [row("T-1", "CAPEX", "-10")]
    prepare_rows(raw, {"amount_override": {"T-1": "-20"}, "reclass": [{"txn": "T-1", "to": "RENT"}]})
    assert raw[0]["amt"] == Decimal("-10") and raw[0]["cat"] == "CAPEX"


def test_prepare_rows_amount_override_revives_row_without_amount():
    """Сумма в леджере пустая — строка непригодна, пока записка казначейства
    не восстановит её (TXN-P7-0033, TXN-P8-0031)."""
    raw = [row("T-1", "TAX", None)]
    assert prepare_rows(raw, {}) == []
    revived = prepare_rows(raw, {"amount_override": {"T-1": "-486204.19"}})
    assert [r["amt"] for r in revived] == [Decimal("-486204.19")]
