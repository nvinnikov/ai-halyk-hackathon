"""Извлечение с цитатой на каждый факт; слияние документов детерминировано."""

import facts_extract

DOSSIER = {
    "account_id": "ACC-1",
    "scenario_id": "S1",
    "docs": [
        {"file": "kyc.pdf", "doc_type": "kyc", "date": "2025-01-01", "text": "kyc text"},
        {"file": "memo.pdf", "doc_type": "treasury_memo", "date": "2025-02-01", "text": "memo text"},
    ],
    "docs_rejected": [],
    "quarantined": [],
}


def empty():
    return {
        "related_parties": [],
        "unrestricted_subsidiaries": [],
        "reclassifications": [],
        "excluded_txns": [],
        "amount_corrections": [],
        "fx_rates": [],
        "numeric_facts": [],
    }


def test_merge_and_contract(tmp_path, monkeypatch):
    def fake_call(prompt, schema, schema_version, **kw):
        if "kyc text" in prompt:
            return {
                **empty(),
                "related_parties": [{"name": "Ertis Capital LLP", "quote": "KYC: связан"}],
            }
        return {
            **empty(),
            "amount_corrections": [
                {"txn_id": "TXN-S1-1", "corrected_amount": "-486204.19", "quote": "записка"}
            ],
            "numeric_facts": [{"key": "severance_liability", "value": "918447.52", "quote": "пособия"}],
            "fx_rates": [
                {
                    "currency": "EUR",
                    "usd_per_unit": "1.16",
                    "effective_from": "",
                    "effective_to": "",
                    "source_quote": "курс",
                    "derivation": "table",
                }
            ],
        }

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    assert facts["related_parties"] == ["Ertis Capital LLP"]
    assert facts["related_quotes"]["Ertis Capital LLP"] == "KYC: связан"
    assert facts["amount_override"] == {"TXN-S1-1": "-486204.19"}
    assert facts["doc_facts"]["severance_liability"] == "918447.52"
    assert facts["fx_rates"][0]["doc_date"] == "2025-02-01"
    assert facts["fx_rates"][0]["doc_hash"]  # заполнен из имени файла-источника


def test_addbacks_assembled_in_numeric_order(tmp_path, monkeypatch):
    def fake_call(prompt, schema, schema_version, **kw):
        if "kyc text" in prompt:
            return empty()
        return {
            **empty(),
            "numeric_facts": [
                {"key": "ebitda_addback_1", "value": "1000000.00", "quote": "q1"},
                {"key": "ebitda_addback_2", "value": "251338.94", "quote": "q2"},
                {"key": "ebitda_addback_materiality", "value": "300000.00", "quote": "qm"},
            ],
        }

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    # Сортировка численная, не лексикографическая ("1000000.00" < "251338.94" как строки)
    assert facts["ebitda_addbacks"] == ["251338.94", "1000000.00"]
    assert facts["addback_materiality"] == "300000.00"


def test_invalid_reclass_category_dropped_with_alarm(tmp_path, monkeypatch):
    """Выдуманная категория не должна тихо выкидывать строку из всех агрегатов."""

    def fake_call(prompt, schema, schema_version, **kw):
        if "kyc text" in prompt:
            return empty()
        return {
            **empty(),
            "reclassifications": [
                {"txn_id": "T-1", "counterparty": None, "to_category": "MADE_UP", "quote": "q"},
                {"txn_id": "T-2", "counterparty": None, "to_category": "INTEREST", "quote": "q2"},
            ],
        }

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    assert [rc["txn"] for rc in facts["reclass"]] == ["T-2"]
    assert any(a["kind"] == "invalid_reclass_category" for a in facts["alarms"])


def test_conflicting_numeric_fact_alarms(tmp_path, monkeypatch):
    def fake_call(prompt, schema, schema_version, **kw):
        val = "1" if "kyc text" in prompt else "2"
        return {**empty(), "numeric_facts": [{"key": "group_capex", "value": val, "quote": "q"}]}

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    assert any(a["kind"] == "doc_fact_conflict" for a in facts["alarms"])


def test_schema_failure_gives_empty_facts_with_alarm(tmp_path, monkeypatch):
    def fake_call(prompt, schema, schema_version, **kw):
        raise facts_extract.llm.SchemaRejected("bad")

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    assert facts["related_parties"] == []
    assert any(a["kind"] == "facts_extraction_failed" for a in facts["alarms"])


def test_resolve_doc_fact(tmp_path, monkeypatch):
    def fake_call(prompt, schema, schema_version, **kw):
        return {"found": True, "value": "9450000.00", "quote": "консолидированный CapEx"}

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    got = facts_extract.resolve_doc_fact(tmp_path, DOSSIER, "group_capex", "CapEx Группы")
    assert got == {"value": "9450000.00", "quote": "консолидированный CapEx"}
