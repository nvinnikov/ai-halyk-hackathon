"""Эталон восстановился из PDF? Имена сравниваются токенами, числа — точно."""

from extraction_eval import diff_facts, diff_specs

WANT_FACTS = {  # формат expected_extraction.FACTS
    "related_parties": ["Ertis Capital LLP"],
    "reclass": [{"txn": "TXN-B1-0020", "to": "INTEREST"}],
}
GOT_FACTS = {  # формат facts_extract
    "related_parties": ["Ertis Capital, LLP"],
    "reclass": [{"txn": "TXN-B1-0020", "counterparty": None, "to": "INTEREST", "quote": "q"}],
    "exclude": [],
    "amount_override": {},
    "fx_rates": [],
    "doc_facts": {},
}


def test_diff_facts_empty_on_token_equal_names():
    assert diff_facts(GOT_FACTS, WANT_FACTS) == []


def test_diff_facts_catches_lost_reclass():
    got = {**GOT_FACTS, "reclass": []}
    d = diff_facts(got, WANT_FACTS)
    assert d and "reclass" in d[0]


def test_diff_facts_catches_extra_related():
    got = {**GOT_FACTS, "related_parties": ["Ertis Capital LLP", "Ghost Co"]}
    assert any("related" in x for x in diff_facts(got, WANT_FACTS))


def test_diff_specs_threshold_and_direction():
    want = {"6.1": ("group_capex_to_ebitda", "max", 9.00)}
    got_ok = {
        "6.1": {"direction": "max", "limit": "9.00", "template": "group_capex_to_ebitda", "valid": True}
    }
    assert diff_specs(got_ok, want) == []
    got_shifted = {"6.1": {**got_ok["6.1"], "limit": "6.50"}}
    assert any("limit" in x for x in diff_specs(got_shifted, want))
    got_dir = {"6.1": {**got_ok["6.1"], "direction": "min"}}
    assert any("direction" in x for x in diff_specs(got_dir, want))


def test_diff_specs_missing_clause():
    assert any("6.1" in x for x in diff_specs({}, {"6.1": ("capex", "max", 2e6)}))


# Тесты на fx_rates
def test_diff_facts_fx_rates_empty():
    want = {"fx_rates": []}
    got = {"fx_rates": []}
    assert diff_facts(got, want) == []


def test_diff_facts_fx_rates_caught():
    want = {"fx_rates": [{"currency": "EUR", "usd_per_unit": "1.1234"}]}
    got = {
        "fx_rates": [{"currency": "EUR", "usd_per_unit": "1.1235", "effective_from": "", "effective_to": ""}]
    }
    # Разница 1e-4 должна быть допущена
    d = diff_facts(got, want)
    assert d == []

    # Разница больше 1e-4 должна быть поймана
    got_bad = {
        "fx_rates": [{"currency": "EUR", "usd_per_unit": "1.1240", "effective_from": "", "effective_to": ""}]
    }
    d = diff_facts(got_bad, want)
    assert any("fx_rates" in x for x in d)


def test_diff_facts_fx_rates_missing():
    want = {"fx_rates": [{"currency": "EUR", "usd_per_unit": "1.1234"}]}
    got = {"fx_rates": []}
    d = diff_facts(got, want)
    assert any("fx_rates" in x for x in d)


def test_diff_facts_fx_rates_extra():
    """Лишний курс в got при пустом want должен быть поймана."""
    want = {"fx_rates": []}
    got = {"fx_rates": [{"currency": "EUR", "usd_per_unit": "1.1234"}]}
    d = diff_facts(got, want)
    assert any("fx_rates" in x and "extra" in x for x in d)


def test_diff_facts_ebitda_addbacks_equal():
    want = {"ebitda_addbacks": ["100.00", "200.50", "300.75"]}
    got = {"ebitda_addbacks": ["100.00", "200.50", "300.75"]}
    assert diff_facts(got, want) == []


def test_diff_facts_ebitda_addbacks_tolerance():
    want = {"ebitda_addbacks": ["100.00", "200.00"]}
    # Разница 0.01 должна быть допущена
    got = {"ebitda_addbacks": ["100.005", "200.005"]}
    d = diff_facts(got, want)
    assert d == []

    # Разница больше 0.01 должна быть поймана
    got_bad = {"ebitda_addbacks": ["100.02", "200.00"]}
    d = diff_facts(got_bad, want)
    assert any("ebitda_addbacks" in x for x in d)


def test_diff_facts_ebitda_addbacks_multiset():
    want = {"ebitda_addbacks": ["100.00", "200.00", "300.00"]}
    got = {
        "ebitda_addbacks": ["300.00", "100.00", "200.00"]  # Разный порядок
    }
    # Должны считаться равными (мультимножество)
    assert diff_facts(got, want) == []

    # Разное количество элементов
    got_bad = {"ebitda_addbacks": ["100.00", "200.00"]}
    d = diff_facts(got_bad, want)
    assert any("ebitda_addbacks" in x for x in d)


def test_diff_facts_addback_materiality_exact():
    # Materiality проверяется только если есть want.ebitda_addbacks
    want = {"ebitda_addbacks": ["100.00", "200.00"], "addback_materiality": "300000.00"}
    got = {"ebitda_addbacks": ["100.00", "200.00"], "addback_materiality": "300000.00"}
    assert diff_facts(got, want) == []

    got_bad = {"ebitda_addbacks": ["100.00", "200.00"], "addback_materiality": "300000.01"}
    d = diff_facts(got_bad, want)
    assert any("addback_materiality" in x for x in d)


# Тесты на детект галлюцинаций: лишние поля в got при пустом want
def test_diff_facts_hallucination_reclass():
    """Extraction выписал reclass, хотя want его не требует."""
    want = {"reclass": []}
    got = {"reclass": [{"txn": "TXN-0001", "counterparty": None, "to": "INTEREST", "quote": "q"}]}
    d = diff_facts(got, want)
    assert any("reclass" in x for x in d)


def test_diff_facts_hallucination_exclude():
    """Extraction выписал exclude, хотя want его не требует."""
    want = {"exclude": []}
    got = {"exclude": ["TXN-0001"]}
    d = diff_facts(got, want)
    assert any("exclude" in x for x in d)


def test_diff_facts_hallucination_amount_override():
    """Extraction выписал amount_override, хотя want его не требует."""
    want = {"amount_override": {}}
    got = {"amount_override": {"TXN-0001": "1000.00"}}
    d = diff_facts(got, want)
    assert any("amount_override" in x for x in d)


def test_diff_facts_hallucination_ebitda_addbacks():
    """Extraction выписал ebitda_addbacks, хотя want их не требует."""
    want = {"ebitda_addbacks": []}
    got = {"ebitda_addbacks": ["1000.00", "2000.00"]}
    d = diff_facts(got, want)
    assert any("ebitda_addbacks" in x for x in d)


def test_diff_facts_addback_materiality_default_when_no_addbacks():
    """Дефолт addback_materiality='0' допущен при отсутствии want.ebitda_addbacks."""
    # Реальный сценарий: got из _empty_facts имеет "0", want не требует addbacks
    want = {}  # Нет ключа ebitda_addbacks
    got = {"addback_materiality": "0"}
    assert diff_facts(got, want) == []


def test_diff_facts_addback_materiality_hallucination_without_addbacks():
    """Ненулевой materiality без эталонных addbacks — галлюцинация."""
    want = {}  # Нет ebitda_addbacks
    got = {"addback_materiality": "0.05"}
    d = diff_facts(got, want)
    assert any("addback_materiality" in x for x in d)


def test_diff_facts_addback_materiality_with_addbacks():
    """Materiality проверяется точно при наличии want.ebitda_addbacks."""
    want = {"ebitda_addbacks": ["100.00"], "addback_materiality": "50000.00"}
    got = {"ebitda_addbacks": ["100.00"], "addback_materiality": "50000.00"}
    assert diff_facts(got, want) == []

    got_bad = {"ebitda_addbacks": ["100.00"], "addback_materiality": "60000.00"}
    d = diff_facts(got_bad, want)
    assert any("addback_materiality" in x for x in d)
