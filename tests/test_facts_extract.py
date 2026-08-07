"""Извлечение с цитатой на каждый факт; слияние документов детерминировано."""

import facts_extract

DOSSIER = {
    "account_id": "ACC-1",
    "scenario_id": "S1",
    "docs": [
        # Тексты содержат цитаты фактов: непроверяемая цитата отбрасывает факт (guard).
        {"file": "kyc.pdf", "doc_type": "kyc", "date": "2025-01-01", "text": "kyc text KYC: связан q"},
        {
            "file": "memo.pdf",
            "doc_type": "treasury_memo",
            "date": "2025-02-01",
            "text": "memo text записка пособия курс q q1 q2 qm консолидированный CapEx $9,450,000.00",
        },
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


def test_paired_payment_fx_rate_has_no_interval_bounds(tmp_path, monkeypatch):
    # paired_payment — курс, выведенный из ОДНОЙ пары зеркальных платежей, а
    # не прочитанный из таблицы с интервалом действия: у него нет и не может
    # быть документально заявленной границы. Модель иногда всё равно
    # проставляет в effective_from/to дату платежа — тогда курс покрывает
    # только один день и не работает донором для остальных дат/сценариев
    # (real-run finding: EUR-курс P3, выведенный из платежа за 2025-12-31,
    # переставал закрывать fx_uncovered на других датах).
    def fake_call(prompt, schema, schema_version, **kw):
        if "kyc text" in prompt:
            return empty()
        return {
            **empty(),
            "fx_rates": [
                {
                    "currency": "EUR",
                    "usd_per_unit": "1.16",
                    "effective_from": "2025-02-01",
                    "effective_to": "2025-02-01",
                    "source_quote": "курс",
                    "derivation": "paired_payment",
                }
            ],
        }

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    rate = facts["fx_rates"][0]
    assert rate["effective_from"] == ""
    assert rate["effective_to"] == ""


def test_table_fx_rate_keeps_its_interval_bounds(tmp_path, monkeypatch):
    # У derivation=table курс читается из таблицы с явным периодом действия —
    # границы документально заявлены и снимать их нельзя.
    def fake_call(prompt, schema, schema_version, **kw):
        if "kyc text" in prompt:
            return empty()
        return {
            **empty(),
            "fx_rates": [
                {
                    "currency": "EUR",
                    "usd_per_unit": "1.16",
                    "effective_from": "2025-02-01",
                    "effective_to": "2025-02-28",
                    "source_quote": "курс",
                    "derivation": "table",
                }
            ],
        }

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    rate = facts["fx_rates"][0]
    assert rate["effective_from"] == "2025-02-01"
    assert rate["effective_to"] == "2025-02-28"


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
        return {"found": True, "value": "9450000.00", "quote": "консолидированный CapEx $9,450,000.00"}

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    got = facts_extract.resolve_doc_fact(tmp_path, DOSSIER, "group_capex", "CapEx Группы")
    assert got == {"value": "9450000.00", "quote": "консолидированный CapEx $9,450,000.00"}


def test_resolve_doc_fact_number_must_be_in_quote(tmp_path, monkeypatch):
    """Ревью PR #9 (3-я волна): число обязано присутствовать в верифицированной
    цитате — как _limit_in_quote для порогов спек; иначе факт отбрасывается."""

    def fake_call(prompt, schema, schema_version, **kw):
        return {"found": True, "value": "9450000.00", "quote": "консолидированный CapEx"}

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    assert facts_extract.resolve_doc_fact(tmp_path, DOSSIER, "group_capex2", "CapEx Группы") is None


def test_unverified_quote_drops_fact_with_alarm(tmp_path, monkeypatch):
    """Цитата не из текста — инъекция или галлюцинация: факт отбрасывается."""

    def fake_call(prompt, schema, schema_version, **kw):
        if "kyc text" in prompt:
            return {
                **empty(),
                "related_parties": [{"name": "Fake LLP", "quote": "цитаты такой в тексте нет"}],
            }
        return empty()

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    assert facts["related_parties"] == []
    assert any(a["kind"] == "quote_unverified" for a in facts["alarms"])


def test_garbage_number_dropped_with_alarm(tmp_path, monkeypatch):
    def fake_call(prompt, schema, schema_version, **kw):
        if "kyc text" in prompt:
            return empty()
        return {
            **empty(),
            "amount_corrections": [{"txn_id": "T-1", "corrected_amount": "N/A", "quote": "записка"}],
        }

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    assert facts["amount_override"] == {}
    assert any(a["kind"] == "invalid_number" for a in facts["alarms"])
