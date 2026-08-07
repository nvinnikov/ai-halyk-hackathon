"""Эталон восстановился из PDF? Имена сравниваются токенами, числа — точно."""

from pathlib import Path

import extraction_eval
import pytest
from extraction_eval import diff_facts, diff_specs, main

import llm
from specs_extract import SPECS_STAGE_VERSION
from util import stable_json

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


def test_diff_specs_with_real_extracted_format():
    """Интеграционный тест: diff_specs работает с реальным форматом clauses из extract_specs."""
    # Реальный формат clauses (с валидацией из extract_specs)
    want = {"6.1": ("icr", "min", 2.00)}
    got_clauses = {
        "6.1": {
            "clause": "6.1",
            "quote": "Interest coverage ratio",
            "metric": "icr",
            "direction": "min",
            "limit": "2.00",
            "trigger": None,
            "confidence": 0.95,
            "valid": True,
            "template": "icr",
            "errors": [],
        }
    }
    # diff_specs читает только нужные поля: direction, limit, template
    assert diff_specs(got_clauses, want) == []


def test_main_builds_clauses_from_raw_specs_artifact(tmp_path, monkeypatch, capsys):
    """Регрессия на commit 2781a9e: main() падал KeyError 'clauses', потому что
    читал specs/<ACC>.json напрямую, хотя на диске лежит СЫРОЙ артефакт модели
    ({"covenants": [...], "alarms": [...]}) — clauses с валидацией собираются
    только через specs_extract.extract_specs(). Тест гоняет main() (не diff_specs
    напрямую) на реальной форме work-каталога, чтобы откат фикса ловился здесь.

    Никакого обращения к LLM: артефакты уже на диске с актуальным stage_version,
    extract_specs() обязан вернуть их из кэша артефакта, не вызывая llm.call.
    """

    def _fail_llm(*args, **kwargs):
        pytest.fail("LLM must not be called: артефакты должны читаться из кэша без модели")

    monkeypatch.setattr(llm, "call", _fail_llm)

    scenario, acc = "TEST", "ACC-TEST"
    # Заведомое расхождение: эталон требует limit=2.00, сырой covenant несёт 2.50 —
    # так тест доказывает, что clauses реально построились из сырого артефакта
    # и дошли до сравнения (а не просто "main() не упал").
    monkeypatch.setattr(extraction_eval, "FACTS", {scenario: {"related_parties": ["Ertis Capital LLP"]}})
    monkeypatch.setattr(extraction_eval, "SPECS", {scenario: {"6.1": ("icr", "min", 2.00)}})

    quote = "Коэффициент покрытия процентов ICR должен быть не менее 2.00."
    agreement_text = f"Статья 6 — Финансовые ковенанты. Пункт 6.1. {quote}"

    index = {"scenario_to_account": {scenario: acc}}
    (tmp_path / "index.json").write_text(stable_json(index))

    facts = {
        "_meta": {"stage_version": 1},
        "related_parties": ["Ertis Capital LLP"],
        "related_quotes": {"Ertis Capital LLP": "q"},
        "unrestricted_subsidiaries": [],
        "subsidiary_quotes": {},
        "reclass": [],
        "exclude": [],
        "exclude_quotes": {},
        "amount_override": {},
        "override_quotes": {},
        "fx_rates": [],
        "doc_facts": {},
        "doc_fact_quotes": {},
        "ebitda_addbacks": [],
        "addback_materiality": "0",
        "alarms": [],
    }
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / f"{acc}.json").write_text(stable_json(facts))

    # Сырой артефакт specs — ровно то, что кладёт на диск specs_extract.py:
    # {"covenants": [...], "alarms": [...]}, без ключа "clauses".
    specs_raw = {
        "_meta": {"stage_version": SPECS_STAGE_VERSION},
        "covenants": [
            {
                "clause": "6.1",
                "quote": quote,
                "metric": "doc(icr)",
                "direction": "min",
                "limit": "2.50",
                "trigger": None,
                "confidence": 0.9,
            }
        ],
        "alarms": [],
    }
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / f"{acc}.json").write_text(stable_json(specs_raw))

    dossier = {
        "_meta": {"stage_version": 4},
        "account_id": acc,
        "docs": [{"date": "2025-01-01", "doc_type": "agreement", "file": "test.pdf", "text": agreement_text}],
        "docs_rejected": [],
        "quarantined": [],
    }
    dossier_dir = tmp_path / "dossier"
    dossier_dir.mkdir()
    (dossier_dir / f"{acc}.json").write_text(stable_json(dossier))

    rc = main(Path("unused.zip"), wd=tmp_path)

    out = capsys.readouterr().out
    assert "Summary" in out
    # limit 2.50 в артефакте vs 2.00 в эталоне — расхождение обязано быть напечатано,
    # доказывая, что clauses построились из сырых covenants и дошли до diff_specs.
    assert "limit" in out
    assert "2.50" in out and "2.0" in out
    # facts совпали (related_parties токен-равны), поэтому провалились именно specs.
    assert "facts: OK" in out
    assert rc == 1  # specs-расхождение не даёт чистый прогон
