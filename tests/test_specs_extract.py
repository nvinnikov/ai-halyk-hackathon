"""Спека валидируется грамматикой при извлечении; сигнатура матчится с шаблоном."""

import pytest

import specs_extract


def covenant(
    clause="6.1", metric="agg(CAPEX, out)", direction="max", limit="2000000", trigger=None, quote=None
):
    quote = quote if quote is not None else f"Пункт {clause} порог {limit}"
    return {
        "clause": clause,
        "quote": quote,
        "metric": metric,
        "direction": direction,
        "limit": limit,
        "trigger": trigger,
        "confidence": 0.9,
    }


def make_dossier(*quotes: str) -> dict:
    # Договор — конкатенация цитат ковенантов: verify_quote видит их как подстроку.
    text = "Кредитный договор. " + " ".join(quotes)
    return {
        "account_id": "ACC-1",
        "scenario_id": "S1",
        "docs": [{"file": "a.pdf", "doc_type": "agreement", "date": "2025-01-01", "text": text}],
        "rejected": [],
        "quarantined_files": [],
    }


def test_valid_spec_with_template_match(tmp_path, monkeypatch):
    cov = covenant()
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(cov["quote"]), set())
    sp = art["clauses"]["6.1"]
    assert sp["valid"] is True and sp["errors"] == []
    assert sp["template"] == "capex"


def test_invalid_dsl_marked_not_dropped(tmp_path, monkeypatch):
    cov = covenant(metric="__import__('os')")
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(cov["quote"]), set())
    sp = art["clauses"]["6.1"]
    assert sp["valid"] is False and sp["errors"]
    assert sp["quote"] == cov["quote"]  # цитата сохранена для эвристики лестницы


def test_unknown_doc_key_invalid_until_resolved(tmp_path, monkeypatch):
    cov = covenant(metric="ratio(doc(group_capex), agg(REVENUE, in))")
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(cov["quote"]), set())
    sp = art["clauses"]["6.1"]
    assert sp["valid"] is False
    assert sp["missing_doc_keys"] == ["group_capex"]


def test_no_agreement_alarm(tmp_path):
    dossier = {"account_id": "ACC-1", "scenario_id": "S1", "docs": []}
    art = specs_extract.extract_specs(tmp_path, dossier, set())
    assert art["clauses"] == {}
    assert any(a["kind"] == "no_agreement" for a in art["alarms"])


def test_trigger_parsed(tmp_path, monkeypatch):
    cov = covenant(
        metric="ratio(agg(FINANCING, in), sub(agg(REVENUE, in), agg(OTHER_OPEX, out)))",
        limit="1.70",
        trigger="gt(agg(FINANCING, in), const(4000000))",
    )
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(cov["quote"]), set())
    assert art["clauses"]["6.1"]["valid"] is True


@pytest.mark.parametrize(
    ("raw_clause", "expected_key"),
    [
        ("Пункт 7.2", "7.2"),
        ("п. 6.1", "6.1"),
        ("Article 6.1", "6.1"),
        ("6.1.", "6.1"),
    ],
)
def test_clause_number_normalized_across_model_formats(tmp_path, monkeypatch, raw_clause, expected_key):
    # Замер мутаций: модель то отдаёт голый номер, то с префиксом «Пункт»/«п.»/
    # «Article» — harness ищет ячейку точным совпадением ключа, без нормализации
    # больше половины ячеек приватного набора уехали бы в фолбэк.
    cov = covenant(clause=raw_clause)
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(cov["quote"]), set())
    assert expected_key in art["clauses"]
    assert art["clauses"][expected_key]["clause"] == expected_key


def test_trigger_period_is_not_a_trigger_and_gets_discarded(tmp_path, monkeypatch):
    # Срок действия договора — не триггер; кривой trigger не должен стоить ячейку.
    cov = covenant(trigger="период с 2025-01-01 по 2025-12-31")
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(cov["quote"]), set())
    sp = art["clauses"]["6.1"]
    assert sp["valid"] is True
    assert sp["trigger"] is None
    assert any(a["kind"] == "trigger_discarded" for a in art["alarms"])


def test_quote_unverified_marks_invalid(tmp_path, monkeypatch):
    cov = covenant(quote="цитата, которой нет в договоре")
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    dossier = make_dossier("Пункт 6.1 порог 2000000")  # текст без этой цитаты
    art = specs_extract.extract_specs(tmp_path, dossier, set())
    sp = art["clauses"]["6.1"]
    assert sp["valid"] is False
    assert "quote_unverified" in sp["errors"]


def test_limit_not_in_quote_marks_invalid(tmp_path, monkeypatch):
    cov = covenant(quote="Пункт 6.1: капитальные затраты не должны превышать установленный предел")
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(cov["quote"]), set())
    sp = art["clauses"]["6.1"]
    assert sp["valid"] is False
    assert "limit_not_in_quote" in sp["errors"]


def test_limit_outlier_alarm_does_not_block_validity(tmp_path, monkeypatch):
    covs = [
        covenant(clause="6.1", metric="agg(CAPEX, out)", limit="2000000"),
        covenant(clause="6.2", metric="agg(PAYROLL, out)", limit="2100000"),
        covenant(clause="6.3", metric="agg(TAX, out)", limit="20"),
    ]
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": covs})
    dossier = make_dossier(*[c["quote"] for c in covs])
    art = specs_extract.extract_specs(tmp_path, dossier, set())
    outliers = [a for a in art["alarms"] if a["kind"] == "limit_outlier"]
    assert [a["clause"] for a in outliers] == ["6.3"]
    assert art["clauses"]["6.3"]["valid"] is True
