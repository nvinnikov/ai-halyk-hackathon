"""Извлечение с цитатой на каждый факт; слияние документов детерминировано."""

from decimal import Decimal

import facts_extract
from facts_extract import _effective_shares

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


EMPTY_OWNERSHIP = {"shares": [], "threshold_percent": "", "threshold_quote": ""}


def facts_only(fn):
    """Фейк LLM, у которого спрошено только про факты: на вызов таблицы
    владения отвечает пустотой, то есть порог не применяется."""

    def call(prompt, schema, schema_version, **kw):
        if schema_version == facts_extract.OWNERSHIP_SCHEMA_VERSION:
            return EMPTY_OWNERSHIP
        return fn(prompt, schema, schema_version, **kw)

    return call


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

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
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

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
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

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
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

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
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

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    assert [rc["txn"] for rc in facts["reclass"]] == ["T-2"]
    assert any(a["kind"] == "invalid_reclass_category" for a in facts["alarms"])


def test_conflicting_numeric_fact_alarms(tmp_path, monkeypatch):
    # Ключ нейтральный: group_capex здесь больше не годится — его отсеивает
    # _merge_doc, потому что его считает код (ревью PR #23, вторая волна).
    def fake_call(prompt, schema, schema_version, **kw):
        val = "1" if "kyc text" in prompt else "2"
        return {**empty(), "numeric_facts": [{"key": "severance_liability", "value": val, "quote": "q"}]}

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    assert any(a["kind"] == "doc_fact_conflict" for a in facts["alarms"])


def test_schema_failure_gives_empty_facts_with_alarm(tmp_path, monkeypatch):
    def fake_call(prompt, schema, schema_version, **kw):
        raise facts_extract.llm.SchemaRejected("bad")

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    assert facts["related_parties"] == []
    assert any(a["kind"] == "facts_extraction_failed" for a in facts["alarms"])


def test_resolve_doc_fact(tmp_path, monkeypatch):
    def fake_call(prompt, schema, schema_version, **kw):
        return {"found": True, "value": "9450000.00", "quote": "консолидированный CapEx $9,450,000.00"}

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
    got = facts_extract.resolve_doc_fact(tmp_path, DOSSIER, "group_capex", "CapEx Группы")
    # Цитата живёт в записке казначейства, договора в досье нет — источник
    # оправдывает факт перед эхо-гардом.
    assert got == {
        "value": "9450000.00",
        "quote": "консолидированный CapEx $9,450,000.00",
        "quote_outside_agreement": True,
    }


def test_resolve_doc_fact_quote_from_agreement_is_not_exonerated(tmp_path, monkeypatch):
    """Атрибуция источника (ревью пост-мержа PR #26): цитата, верифицируемая в
    тексте ДОГОВОРА, оправдания не получает — эхо-гард вправе счесть её эхом
    порога. Оправдание — только положительная улика: цитата вне договора и ни
    в одном договоре."""
    dossier = {
        **DOSSIER,
        "docs": DOSSIER["docs"]
        + [
            {
                "file": "agreement.pdf",
                "doc_type": "agreement",
                "date": "2025-01-01",
                "text": "договор: покрытие не менее $9,450,000.00 обязательно",
            }
        ],
    }

    def fake_call(prompt, schema, schema_version, **kw):
        return {"found": True, "value": "9450000.00", "quote": "не менее $9,450,000.00"}

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
    got = facts_extract.resolve_doc_fact(tmp_path, dossier, "cov_floor", "минимальное покрытие")
    assert got is not None and got["quote_outside_agreement"] is False


def test_resolve_doc_fact_number_must_be_in_quote(tmp_path, monkeypatch):
    """Ревью PR #9 (3-я волна): число обязано присутствовать в верифицированной
    цитате — как _limit_in_quote для порогов спек; иначе факт отбрасывается."""

    def fake_call(prompt, schema, schema_version, **kw):
        return {"found": True, "value": "9450000.00", "quote": "консолидированный CapEx"}

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
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

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
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

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    assert facts["amount_override"] == {}
    assert any(a["kind"] == "invalid_number" for a in facts["alarms"])


def test_resolve_doc_fact_negative_value_matches_unsigned_quote(tmp_path, monkeypatch):
    """Ревью PR #9 (8-я волна): отрицательный doc-факт сверяется с цитатой по
    модулю — знак в вёрстке живёт словом («минус») или скобками."""

    def fake_call(prompt, schema, schema_version, **kw):
        return {"found": True, "value": "-9450000.00", "quote": "консолидированный CapEx $9,450,000.00"}

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
    got = facts_extract.resolve_doc_fact(tmp_path, DOSSIER, "group_capex3", "CapEx Группы")
    assert got == {
        "value": "-9450000.00",
        "quote": "консолидированный CapEx $9,450,000.00",
        "quote_outside_agreement": True,
    }


def test_resolve_doc_fact_accepts_percent_of_statute(tmp_path, monkeypatch):
    """«N% of/от <статья>» — не число, но опознаваемый процентный кэп
    (rewrites.parse_percent_of_statute): _number_ok такую строку отсеивает,
    и гейт резолва обязан пропустить её отдельно, иначе адресно найденный
    процентный кэп теряется целиком (задача про процентный кэп из doc-ключа)."""
    dossier = {
        **DOSSIER,
        "docs": DOSSIER["docs"]
        + [
            {
                "file": "agreement.pdf",
                "doc_type": "agreement",
                "date": "2025-01-01",
                "text": "добавления не превышают 5% of Revenue по условиям договора",
            }
        ],
    }

    def fake_call(prompt, schema, schema_version, **kw):
        return {"found": True, "value": "5% of Revenue", "quote": "добавления не превышают 5% of Revenue"}

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
    got = facts_extract.resolve_doc_fact(tmp_path, dossier, "ebitda_addback_limit", "потолок добавок")
    assert got == {
        "value": "5% of Revenue",
        "quote": "добавления не превышают 5% of Revenue",
        "quote_outside_agreement": False,
    }


def test_resolve_doc_fact_rejects_non_numeric_non_percent_value(tmp_path, monkeypatch):
    """Строка вроде «3.00x» (порог с суффиксом кратности) не подходит ни под
    число, ни под «N% статьи» — резолв обязан отказать, как и раньше:
    расширение гейта не имеет права впустить произвольный текст."""
    dossier = {
        **DOSSIER,
        "docs": DOSSIER["docs"]
        + [
            {
                "file": "agreement.pdf",
                "doc_type": "agreement",
                "date": "2025-01-01",
                "text": "порог 3.00x применяется",
            }
        ],
    }

    def fake_call(prompt, schema, schema_version, **kw):
        return {"found": True, "value": "3.00x", "quote": "порог 3.00x применяется"}

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
    assert facts_extract.resolve_doc_fact(tmp_path, dossier, "leverage_ratio", "порог") is None


def test_empty_dossier_facts_alarmed_and_not_cached(tmp_path, monkeypatch):
    """Досье без документов — деградация с алярмом no_documents, артефакт не
    пишется: перезапуск после починки конвейера выше перепытается
    (ревью PR #9, 24-я волна)."""

    def no_llm(prompt, schema, schema_version, **kw):
        raise AssertionError("LLM не должен вызываться для пустого досье")

    monkeypatch.setattr(facts_extract.llm, "call", no_llm)
    bare = {"account_id": "ACC-1", "scenario_id": "S1", "docs": []}
    art = facts_extract.extract_facts(tmp_path, bare)
    assert any(a["kind"] == "no_documents" and a["account"] == "ACC-1" for a in art["alarms"])
    assert not (tmp_path / "facts" / "ACC-1.json").exists()


KYC_TEXT = (
    "Организация Доля голосующих прав Ertis Capital, LLP 31.4% "
    "Irtysh Advisory Bureau 18.6% Pavlodar Plant Services LLP 12.5% "
    "Организации, в которых Группа владеет 20.0% и более голосующих прав, "
    "признаются связанными сторонами для целей Договора."
)
OWNERSHIP_DOSSIER = {
    "account_id": "ACC-1",
    "scenario_id": "S1",
    "docs": [{"file": "kyc.pdf", "doc_type": "kyc", "date": "2025-12-31", "text": KYC_TEXT}],
    "docs_rejected": [],
    "quarantined": [],
}


def ownership(shares, threshold="20.0", threshold_quote="Группа владеет 20.0% и более"):
    return {
        "shares": [
            {
                "name": row[0],
                "share_percent": row[1],
                "quote": row[2],
                "held_through": row[3] if len(row) > 3 else "",
            }
            for row in shares
        ],
        "threshold_percent": threshold,
        "threshold_quote": threshold_quote,
    }


def _dispatch(model_related, own):
    """Два вызова на один kyc-документ: факты и таблица владения."""

    def fake_call(prompt, schema, schema_version, **kw):
        if schema_version == facts_extract.OWNERSHIP_SCHEMA_VERSION:
            return own
        return {**empty(), "related_parties": model_related}

    return fake_call


def test_ownership_threshold_applied_by_code(tmp_path, monkeypatch):
    """Сравнение доли с порогом — арифметика, её делает код. Модель вернула
    пустой набор, но таблица и порог написаны в документе, и набор считается
    из них."""
    own = ownership(
        [
            ("Ertis Capital, LLP", "31.4", "Ertis Capital, LLP 31.4%"),
            ("Irtysh Advisory Bureau", "18.6", "Irtysh Advisory Bureau 18.6%"),
        ]
    )
    monkeypatch.setattr(facts_extract.llm, "call", _dispatch([], own))
    facts = facts_extract.extract_facts(tmp_path, OWNERSHIP_DOSSIER)
    assert facts["related_parties"] == ["Ertis Capital, LLP"]
    assert facts["related_quotes"]["Ertis Capital, LLP"] == "Ertis Capital, LLP 31.4%"


def test_ownership_below_threshold_overrides_model(tmp_path, monkeypatch):
    """Таблица долей с порогом старше суждения модели по тем организациям,
    которые в таблице есть: доля ниже порога — не связанная сторона."""
    own = ownership([("Irtysh Advisory Bureau", "18.6", "Irtysh Advisory Bureau 18.6%")])
    model = [{"name": "Irtysh Advisory Bureau", "quote": "Irtysh Advisory Bureau 18.6%"}]
    monkeypatch.setattr(facts_extract.llm, "call", _dispatch(model, own))
    facts = facts_extract.extract_facts(tmp_path, OWNERSHIP_DOSSIER)
    assert facts["related_parties"] == []


def test_ownership_keeps_party_absent_from_table(tmp_path, monkeypatch):
    """Организация, которой в таблице долей нет, порогом не отменяется: её
    связанность раскрыта где-то ещё."""
    own = ownership([("Irtysh Advisory Bureau", "18.6", "Irtysh Advisory Bureau 18.6%")])
    model = [{"name": "Pavlodar Plant Services LLP", "quote": "Pavlodar Plant Services LLP 12.5%"}]
    monkeypatch.setattr(facts_extract.llm, "call", _dispatch(model, own))
    facts = facts_extract.extract_facts(tmp_path, OWNERSHIP_DOSSIER)
    assert facts["related_parties"] == ["Pavlodar Plant Services LLP"]


def test_ownership_share_number_must_be_in_its_quote(tmp_path, monkeypatch):
    """Цитата настоящая, но число доли взято не из неё — факт отбрасывается.

    Тот же инвариант, что у resolve_doc_fact: проверять существование цитаты
    мало, число обязано в ней стоять. Иначе доля 12.5% из документа приезжает
    в расчёт как 31.4% и втягивает организацию в набор связанных сторон.
    """
    own = ownership([("Ertis Capital, LLP", "31.4", "Irtysh Advisory Bureau 18.6%")])
    monkeypatch.setattr(facts_extract.llm, "call", _dispatch([], own))
    facts = facts_extract.extract_facts(tmp_path, OWNERSHIP_DOSSIER)
    assert facts["related_parties"] == []
    assert any(a["kind"] == "invalid_number" and a["field"] == "ownership_share" for a in facts["alarms"])


def test_ownership_threshold_number_must_be_in_its_quote(tmp_path, monkeypatch):
    """То же для порога: цитата из документа, но названного в ней порога нет."""
    own = ownership(
        [("Ertis Capital, LLP", "31.4", "Ertis Capital, LLP 31.4%")],
        threshold_quote="Irtysh Advisory Bureau 18.6%",
    )
    monkeypatch.setattr(facts_extract.llm, "call", _dispatch([], own))
    facts = facts_extract.extract_facts(tmp_path, OWNERSHIP_DOSSIER)
    assert facts["related_parties"] == []
    assert any(a["kind"] == "invalid_number" and a["field"] == "ownership_threshold" for a in facts["alarms"])


ABSORB_TEXT = (
    "Организация Доля голосующих прав Ertis Capital, LLP 12.5% "
    "Организации, в которых Группа владеет 20.0% и более голосующих прав, "
    "признаются связанными сторонами. Примечание 6: операции с "
    "Ertis Capital Trading LLP совершаются на рыночных условиях."
)
ABSORB_DOSSIER = {
    "account_id": "ACC-1",
    "scenario_id": "S1",
    "docs": [{"file": "kyc.pdf", "doc_type": "kyc", "date": "2025-12-31", "text": ABSORB_TEXT}],
    "docs_rejected": [],
    "quarantined": [],
}


def test_ownership_below_threshold_does_not_absorb_other_names(tmp_path, monkeypatch):
    """Снять связанность можно только с той организации, которая в таблице есть.

    is_related матчит подмножество токенов в обе стороны, поэтому короткое имя
    из таблицы вычищало бы более длинные и другие организации: у
    «Ertis Capital, LLP» токены — подмножество токенов
    «Ertis Capital Trading LLP», раскрытой в другом документе. Сравнение
    наборов токенов на равенство переживает пунктуацию юрформы, ради которой
    токены и брались, но чужие имена не поглощает.
    """
    own = ownership([("Ertis Capital, LLP", "12.5", "Ertis Capital, LLP 12.5%")])
    model = [{"name": "Ertis Capital Trading LLP", "quote": "операции с Ertis Capital Trading LLP"}]
    monkeypatch.setattr(facts_extract.llm, "call", _dispatch(model, own))
    facts = facts_extract.extract_facts(tmp_path, ABSORB_DOSSIER)
    assert facts["related_parties"] == ["Ertis Capital Trading LLP"]


TWO_ROW_TEXT = (
    "Организация Доля голосующих прав Ertis Capital, LLP прямая 38.9% "
    "Ertis Capital, LLP косвенная 5.0% "
    "Организации, в которых Группа владеет 20.0% и более голосующих прав, "
    "признаются связанными сторонами."
)
TWO_ROW_DOSSIER = {
    "account_id": "ACC-1",
    "scenario_id": "S1",
    "docs": [{"file": "kyc.pdf", "doc_type": "kyc", "date": "2025-12-31", "text": TWO_ROW_TEXT}],
    "docs_rejected": [],
    "quarantined": [],
}


def test_ownership_number_must_stand_alone_in_quote(tmp_path, monkeypatch):
    """Число обязано стоять в цитате целиком, а не быть куском другого числа.

    Проверка порогов спек подстрочна, и «5» находится внутри «25.0». Заниженный
    порог тише завышенного: организации ниже настоящего порога молча уезжают в
    набор с настоящей цитатой, related-фильтр расширяется на чужие строки, а
    вердикт остаётся правдоподобным. Промах в эту сторону алярма о снятии не
    оставляет — значит ловить надо на входе.
    """
    text = (
        "Организация Доля голосующих прав Ertis Capital, LLP 31.4% "
        "Организации, в которых Группа владеет 25.0% и более голосующих прав, "
        "признаются связанными сторонами."
    )
    dossier = {
        "account_id": "ACC-1",
        "scenario_id": "S1",
        "docs": [{"file": "kyc.pdf", "doc_type": "kyc", "date": "2025-12-31", "text": text}],
        "docs_rejected": [],
        "quarantined": [],
    }
    # Цитата настоящая, но «5» в ней — кусок «25.0», а не самостоятельное число.
    own = ownership(
        [("Ertis Capital, LLP", "31.4", "Ertis Capital, LLP 31.4%")],
        threshold="5",
        threshold_quote="Группа владеет 25.0% и более",
    )
    monkeypatch.setattr(facts_extract.llm, "call", _dispatch([], own))
    facts = facts_extract.extract_facts(tmp_path, dossier)
    assert facts["related_parties"] == []
    assert any(a["kind"] == "invalid_number" and a["field"] == "ownership_threshold" for a in facts["alarms"])


def test_ownership_number_matches_by_value_not_by_spelling(tmp_path, monkeypatch):
    """Модель нормализует число охотнее документа — это не повод отбрасывать факт.

    Документ пишет «25.0%», модель возвращает «25»; документ пишет «31,40%»,
    модель — «31.4». Сверять надо значение, а не запись: проверка по формам
    умеет только снимать хвостовые нули, но не дописывать их под вёрстку
    документа, и отказ здесь стоит дорого — порог без значения отключает
    применение кодом целиком и возвращает набор к суждению модели.
    """
    text = (
        "Организация Доля голосующих прав Ertis Capital, LLP 31,40% "
        "Организации, в которых Группа владеет 25.0% и более голосующих прав, "
        "признаются связанными сторонами."
    )
    dossier = {
        "account_id": "ACC-1",
        "scenario_id": "S1",
        "docs": [{"file": "kyc.pdf", "doc_type": "kyc", "date": "2025-12-31", "text": text}],
        "docs_rejected": [],
        "quarantined": [],
    }
    own = ownership(
        [("Ertis Capital, LLP", "31.4", "Ertis Capital, LLP 31,40%")],
        threshold="25",
        threshold_quote="Группа владеет 25.0% и более",
    )
    monkeypatch.setattr(facts_extract.llm, "call", _dispatch([], own))
    facts = facts_extract.extract_facts(tmp_path, dossier)
    assert facts["related_parties"] == ["Ertis Capital, LLP"]
    assert not [a for a in facts["alarms"] if a["kind"] == "invalid_number"]


def test_ownership_row_above_threshold_wins_over_row_below(tmp_path, monkeypatch):
    """Организация в таблице двумя строками остаётся связанной по большей доле.

    Прямая доля 38.9% и косвенная 5.0% — две строки об одной организации.
    Проход по строкам ниже порога не имеет права снять то, что та же таблица
    признала связанным: иначе организация с долей выше порога выпадает из
    набора, related-фильтр сужается, и цена — статус ячейки целиком. Тот же
    механизм срабатывает на дубле строки в ответе модели.
    """
    own = ownership(
        [
            ("Ertis Capital, LLP", "38.9", "Ertis Capital, LLP прямая 38.9%"),
            ("Ertis Capital, LLP", "5.0", "Ertis Capital, LLP косвенная 5.0%"),
        ]
    )
    monkeypatch.setattr(facts_extract.llm, "call", _dispatch([], own))
    facts = facts_extract.extract_facts(tmp_path, TWO_ROW_DOSSIER)
    assert facts["related_parties"] == ["Ertis Capital, LLP"]


def test_ownership_removal_is_alarmed(tmp_path, monkeypatch):
    """Снятие связанной стороны видно в трейсе: имя, доля и порог.

    Добавление оставляет след в related_quotes, а снятие молчало — в окне
    прогона не было видно ни того, что набор сузился, ни по какой цитате.
    """
    own = ownership([("Irtysh Advisory Bureau", "18.6", "Irtysh Advisory Bureau 18.6%")])
    model = [{"name": "Irtysh Advisory Bureau", "quote": "Irtysh Advisory Bureau 18.6%"}]
    monkeypatch.setattr(facts_extract.llm, "call", _dispatch(model, own))
    facts = facts_extract.extract_facts(tmp_path, OWNERSHIP_DOSSIER)
    assert facts["related_parties"] == []
    alarm = next(a for a in facts["alarms"] if a["kind"] == "ownership_below_threshold")
    assert alarm["name"] == "Irtysh Advisory Bureau"
    assert alarm["share"] == "18.6"
    assert alarm["threshold"] == "20.0"


def test_ownership_call_failure_costs_only_the_threshold(tmp_path, monkeypatch):
    """Сбой вызова таблицы владения — потеря уточнения, а не всех фактов.

    Проход по таблице уточняет набор, который модель уже назвала в общем
    проходе; его падение (бюджет, промах кассеты, сеть) не должно стоить
    заёмщику реклассификаций, курсов и обязательств. Артефакт при этом не
    кэшируется: причина транзиентная, и перезапуск обязан перепытаться."""

    def fake_call(prompt, schema, schema_version, **kw):
        if schema_version == facts_extract.OWNERSHIP_SCHEMA_VERSION:
            raise RuntimeError("нет кассеты для ключа")
        return {
            **empty(),
            "related_parties": [{"name": "Ertis Capital, LLP", "quote": "Ertis Capital, LLP 31.4%"}],
        }

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    facts = facts_extract.extract_facts(tmp_path, OWNERSHIP_DOSSIER)
    assert facts["related_parties"] == ["Ertis Capital, LLP"]
    assert any(a["kind"] == "ownership_extraction_failed" for a in facts["alarms"])
    assert not (tmp_path / "facts" / "ACC-1.json").exists()


def test_ownership_unverifiable_threshold_quote_ignored(tmp_path, monkeypatch):
    """Порог без проверяемой цитаты не применяется — контракт guard. Набор
    остаётся тем, что дала модель, и появляется алярм."""
    own = ownership(
        [("Ertis Capital, LLP", "31.4", "Ertis Capital, LLP 31.4%")],
        threshold_quote="Группа владеет 90% и более",
    )
    monkeypatch.setattr(facts_extract.llm, "call", _dispatch([], own))
    facts = facts_extract.extract_facts(tmp_path, OWNERSHIP_DOSSIER)
    assert facts["related_parties"] == []
    assert any(
        a["kind"] == "quote_unverified" and a["field"] == "ownership_threshold" for a in facts["alarms"]
    )


def _s(name, pct, via=""):
    return {"name": name, "share_percent": Decimal(pct), "held_through": via, "quote": ""}


def test_direct_share_is_itself():
    assert _effective_shares([_s("A LLP", "41.3")]) == {"A LLP": Decimal("41.3")}


def test_indirect_share_is_a_product():
    rows = [_s("Mid LLP", "24.0"), _s("Target LLP", "52.0", via="Mid LLP")]
    got = _effective_shares(rows)
    assert got["Target LLP"] == Decimal("12.48")


def test_three_links_multiply():
    rows = [_s("A", "50"), _s("B", "50", via="A"), _s("C", "50", via="B")]
    assert _effective_shares(rows)["C"] == Decimal("12.5")


def test_unknown_holder_keeps_direct_value_and_does_not_crash():
    got = _effective_shares([_s("X LLP", "30.0", via="Nowhere LLP")])
    assert got["X LLP"] == Decimal("30.0")


def test_cycle_does_not_hang():
    rows = [_s("A", "50", via="B"), _s("B", "50", via="A")]
    got = _effective_shares(rows)
    assert set(got) == {"A", "B"}


def test_largest_path_wins_when_entity_listed_twice():
    rows = [_s("Mid", "20"), _s("T", "10"), _s("T", "90", via="Mid")]
    # прямая 10% против косвенной 18% — берём большую
    assert _effective_shares(rows)["T"] == Decimal("18")


def test_cycle_raises_chain_broken_alarm():
    alarms: list = []
    rows = [_s("A", "50", via="B"), _s("B", "50", via="A")]
    _effective_shares(rows, alarms=alarms)
    alarm = next(a for a in alarms if a["kind"] == "ownership_chain_broken")
    assert alarm["reason"] == "cycle"
    assert alarm["name"] in {"A", "B"}


def test_depth_limit_raises_chain_broken_alarm():
    alarms: list = []
    rows = [
        _s("L0", "50", via="L1"),
        _s("L1", "50", via="L2"),
        _s("L2", "50", via="L3"),
        _s("L3", "50", via="L4"),
        _s("L4", "50", via="L5"),
        _s("L5", "50"),
    ]
    _effective_shares(rows, alarms=alarms)
    alarm = next(a for a in alarms if a["kind"] == "ownership_chain_broken")
    assert alarm["reason"] == "max_depth"
    assert alarm["name"] == "L4"
    assert alarm["held_through"] == "L5"


# --- эффективная доля через полный путь извлечения фактов --------------------

CHAIN_TEXT = (
    "Организация Доля голосующих прав Mid Holding LLP 15.0% "
    "Target LLP 54.0% Direct Party LLP 25.0% "
    "Организации, в которых Группа владеет 20.0% и более голосующих прав, "
    "признаются связанными сторонами для целей Договора. "
    "Доля в Target LLP удерживается косвенно через Mid Holding LLP."
)
CHAIN_DOSSIER = {
    "account_id": "ACC-1",
    "scenario_id": "S1",
    "docs": [{"file": "kyc.pdf", "doc_type": "kyc", "date": "2025-12-31", "text": CHAIN_TEXT}],
    "docs_rejected": [],
    "quarantined": [],
}


def test_effective_share_dilutes_chain_but_keeps_unrelated_direct_share(tmp_path, monkeypatch):
    """Прямая доля Target LLP (54.0%) не ниже порога (20.0%), но она удерживается
    косвенно через Mid Holding LLP (15.0%): эффективная доля 8.10% ниже порога,
    и Target LLP в набор связанных сторон не попадает. Рядом — Direct Party LLP
    с прямой долей 25.0% и без держателя: она обязана попасть, иначе тест не
    отличит сработавшую дилюцию от сломанного разбора по порогу целиком."""
    own = ownership(
        [
            ("Mid Holding LLP", "15.0", "Mid Holding LLP 15.0%"),
            ("Target LLP", "54.0", "Target LLP 54.0%", "Mid Holding LLP"),
            ("Direct Party LLP", "25.0", "Direct Party LLP 25.0%"),
        ]
    )
    monkeypatch.setattr(facts_extract.llm, "call", _dispatch([], own))
    facts = facts_extract.extract_facts(tmp_path, CHAIN_DOSSIER)
    assert facts["related_parties"] == ["Direct Party LLP"]


def test_effective_share_removal_alarm_shows_direct_and_effective(tmp_path, monkeypatch):
    """Модель назвала Target LLP связанной стороной по прямой доле (54.0%);
    таблица раскрывает держателя (Mid Holding LLP, 15.0%), эффективная доля —
    8.10%, ниже порога. Снятие видно алярмом с обеими величинами."""
    own = ownership(
        [
            ("Mid Holding LLP", "15.0", "Mid Holding LLP 15.0%"),
            ("Target LLP", "54.0", "Target LLP 54.0%", "Mid Holding LLP"),
        ]
    )
    model = [{"name": "Target LLP", "quote": "Target LLP 54.0%"}]
    monkeypatch.setattr(facts_extract.llm, "call", _dispatch(model, own))
    facts = facts_extract.extract_facts(tmp_path, CHAIN_DOSSIER)
    assert facts["related_parties"] == []
    dilution = next(a for a in facts["alarms"] if a["kind"] == "ownership_effective_share")
    assert dilution["name"] == "Target LLP"
    assert dilution["direct"] == "54.0"
    assert dilution["effective"] == "8.10"
    removal = next(a for a in facts["alarms"] if a["kind"] == "ownership_below_threshold")
    assert removal["name"] == "Target LLP"


# --- капитальные затраты Группы (документ группового уровня) -----------------

GROUP_TEXT = (
    "Note 7 - Property, Plant and Equipment. "
    "There were no disposals of property, plant and equipment during the year. "
    "Net book value at the beginning of the year $148,028,989.69 "
    "Depreciation charge for the year $15,826,229.43 "
    "Net book value at the end of the year $154,050,122.81"
)
GROUP_DOSSIER = {
    "account_id": "ACC-1",
    "scenario_id": "S1",
    "docs": [
        {
            "file": "group.pdf",
            "doc_type": "financial_notes",
            "date": "2025-12-31",
            "scope": "group",
            "text": GROUP_TEXT,
        }
    ],
    "docs_rejected": [],
    "quarantined": [],
}


def ppe(**over):
    base = {
        "opening_value": "148028989.69",
        "opening_quote": "Net book value at the beginning of the year $148,028,989.69",
        "closing_value": "154050122.81",
        "closing_quote": "Net book value at the end of the year $154,050,122.81",
        "depreciation": "15826229.43",
        "depreciation_quote": "Depreciation charge for the year $15,826,229.43",
        "additions": "",
        "additions_quote": "",
        "no_disposals": True,
        "no_disposals_quote": "There were no disposals of property, plant and equipment during the year",
        "other_movements": False,
        "other_movements_quote": "",
        "currency": "USD",
        "amount_scale": "1",
        "units_quote": "",
    }
    return {**base, **over}


def _group_dispatch(raw):
    def fake_call(prompt, schema, schema_version, **kw):
        assert schema_version == facts_extract.GROUP_PPE_SCHEMA_VERSION
        return raw

    return fake_call


def test_group_capex_computed_by_code(tmp_path, monkeypatch):
    """Модель отдаёт три числа примечания, поступления считает код:
    конец − начало + амортизация."""
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(ppe()))
    facts = facts_extract.extract_facts(tmp_path, GROUP_DOSSIER)
    assert facts["doc_facts"][facts_extract.GROUP_CAPEX_KEY] == "21847362.55"


def test_group_doc_not_read_by_common_facts_pass(tmp_path, monkeypatch):
    """Решения материнской компании не применяются к операциям заёмщика:
    общий проход фактов документ группового уровня не читает вовсе."""
    seen = []

    def fake_call(prompt, schema, schema_version, **kw):
        seen.append(schema_version)
        return ppe()

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    facts_extract.extract_facts(tmp_path, GROUP_DOSSIER)
    assert seen == [facts_extract.GROUP_PPE_SCHEMA_VERSION]


def test_group_capex_needs_no_disposals_clause(tmp_path, monkeypatch):
    """Без оговорки об отсутствии выбытий тождество даёт не поступления —
    расчёта нет, ячейка уходит на лестницу."""
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(ppe(no_disposals=False)))
    facts = facts_extract.extract_facts(tmp_path, GROUP_DOSSIER)
    assert facts_extract.GROUP_CAPEX_KEY not in facts["doc_facts"]
    assert any(a["kind"] == "group_capex_disposals_unconfirmed" for a in facts["alarms"])


def test_group_capex_number_must_be_in_its_quote(tmp_path, monkeypatch):
    """Цитата привязывает число к формулировке: подменённое значение при
    настоящей цитате не принимается."""
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(ppe(closing_value="254050122.81")))
    facts = facts_extract.extract_facts(tmp_path, GROUP_DOSSIER)
    assert facts_extract.GROUP_CAPEX_KEY not in facts["doc_facts"]
    assert any(a["kind"] == "invalid_number" for a in facts["alarms"])


def test_group_capex_stated_additions_win(tmp_path, monkeypatch):
    """Если документ называет поступления отдельным числом — берётся оно,
    восстанавливать их из движения стоимости незачем."""
    raw = ppe(
        additions="21847362.55",
        additions_quote="Additions during the year $21,847,362.55",
        no_disposals=False,
    )
    text = GROUP_TEXT + " Additions during the year $21,847,362.55"
    dossier = {**GROUP_DOSSIER, "docs": [{**GROUP_DOSSIER["docs"][0], "text": text}]}
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(raw))
    facts = facts_extract.extract_facts(tmp_path, dossier)
    assert facts["doc_facts"][facts_extract.GROUP_CAPEX_KEY] == "21847362.55"


def test_group_capex_negative_rejected(tmp_path, monkeypatch):
    """Отрицательных поступлений не бывает: перепутанные начало и конец дали бы
    уверенный COMPLIANT на max-ковенанте.

    Амортизация здесь настоящая, со своей цитатой: с нулём тест проходил бы по
    ложной причине — `_limit_in_quote("0", ...)` совпадает с любой цитатой, где
    есть цифра 0, и инвариант «число стоит в собственной цитате» не проверялся
    бы вовсе (ревью PR #23, вторая волна)."""
    text = GROUP_TEXT + " Net book value at the end of the year $100,000,000.00"
    raw = ppe(
        closing_value="100000000.00",
        closing_quote="Net book value at the end of the year $100,000,000.00",
    )
    dossier = {**GROUP_DOSSIER, "docs": [{**GROUP_DOSSIER["docs"][0], "text": text}]}
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(raw))
    facts = facts_extract.extract_facts(tmp_path, dossier)
    assert facts_extract.GROUP_CAPEX_KEY not in facts["doc_facts"]
    assert any(a["kind"] == "group_capex_non_positive" for a in facts["alarms"])


def test_group_capex_other_movements_block_the_identity(tmp_path, monkeypatch):
    """Тождество движения стоимости держится не только на выбытиях: обесценение
    и курсовые разницы двигают результат так же молча (ревью PR #23)."""
    raw = ppe(
        other_movements=True,
        other_movements_quote="Net book value at the end of the year $154,050,122.81",
    )
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(raw))
    facts = facts_extract.extract_facts(tmp_path, GROUP_DOSSIER)
    assert facts_extract.GROUP_CAPEX_KEY not in facts["doc_facts"]
    assert any(a["kind"] == "group_capex_other_movements" for a in facts["alarms"])


def test_group_capex_other_movements_do_not_block_stated_additions(tmp_path, monkeypatch):
    """Названные поступления читаются, а не выводятся: условия применимости
    тождества к ним не относятся."""
    text = GROUP_TEXT + " Additions during the year $21,847,362.55 Impairment loss $4,000.00"
    raw = ppe(
        additions="21847362.55",
        additions_quote="Additions during the year $21,847,362.55",
        other_movements=True,
        other_movements_quote="Impairment loss $4,000.00",
    )
    dossier = {**GROUP_DOSSIER, "docs": [{**GROUP_DOSSIER["docs"][0], "text": text}]}
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(raw))
    facts = facts_extract.extract_facts(tmp_path, dossier)
    assert facts["doc_facts"][facts_extract.GROUP_CAPEX_KEY] == "21847362.55"


def test_group_capex_stated_negative_rejected(tmp_path, monkeypatch):
    """Проверка знака общая для обеих веток: отрицательный числитель на
    max-ковенанте даёт уверенный COMPLIANT, то есть стоит статуса."""
    text = GROUP_TEXT + " Additions during the year $-21,847,362.55"
    raw = ppe(additions="-21847362.55", additions_quote="Additions during the year $-21,847,362.55")
    dossier = {**GROUP_DOSSIER, "docs": [{**GROUP_DOSSIER["docs"][0], "text": text}]}
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(raw))
    facts = facts_extract.extract_facts(tmp_path, dossier)
    assert facts_extract.GROUP_CAPEX_KEY not in facts["doc_facts"]
    assert any(a["kind"] == "group_capex_non_positive" for a in facts["alarms"])


def test_group_capex_conflict_drops_the_key(tmp_path, monkeypatch):
    """Конечная материнская компания у группы одна: два документа с разными
    значениями — признак лишней привязки, а не повод взять последний."""
    other_text = GROUP_TEXT.replace("154,050,122.81", "160,050,122.81")
    dossier = {
        **GROUP_DOSSIER,
        "docs": [
            GROUP_DOSSIER["docs"][0],
            {**GROUP_DOSSIER["docs"][0], "file": "group2.pdf", "text": other_text},
        ],
    }

    def fake_call(prompt, schema, schema_version, **kw):
        if "160,050,122.81" in prompt:
            return ppe(
                closing_value="160050122.81",
                closing_quote="Net book value at the end of the year $160,050,122.81",
            )
        return ppe()

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    facts = facts_extract.extract_facts(tmp_path, dossier)
    assert facts_extract.GROUP_CAPEX_KEY not in facts["doc_facts"]
    alarm = next(a for a in facts["alarms"] if a["kind"] == "group_capex_conflict")
    assert alarm["values"] == ["21847362.55", "27847362.55"]
    assert alarm["files"] == ["group.pdf", "group2.pdf"]


def test_group_capex_two_documents_agreeing_are_fine(tmp_path, monkeypatch):
    """Совпавшие значения конфликтом не считаются — расхождения нет."""
    dossier = {
        **GROUP_DOSSIER,
        "docs": [
            GROUP_DOSSIER["docs"][0],
            {**GROUP_DOSSIER["docs"][0], "file": "group2.pdf"},
        ],
    }
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(ppe()))
    facts = facts_extract.extract_facts(tmp_path, dossier)
    assert facts["doc_facts"][facts_extract.GROUP_CAPEX_KEY] == "21847362.55"
    assert not any(a["kind"] == "group_capex_conflict" for a in facts["alarms"])


def test_model_group_capex_never_reaches_doc_facts(tmp_path, monkeypatch):
    """Ключ производный: его считает код. FACTS_PROMPT просит его у модели, и
    модель выписывает туда порог из цитаты пункта договора — после перевода
    шаблона на doc(group_capex) такое значение ИСПОЛНЯЕТСЯ (ревью PR #23)."""

    def fake_call(prompt, schema, schema_version, **kw):
        return {**empty(), "numeric_facts": [{"key": "group_capex", "value": "9.00", "quote": "q"}]}

    monkeypatch.setattr(facts_extract.llm, "call", facts_only(fake_call))
    facts = facts_extract.extract_facts(tmp_path, DOSSIER)
    assert facts_extract.GROUP_CAPEX_KEY not in facts["doc_facts"]
    alarm = next(a for a in facts["alarms"] if a["kind"] == "group_capex_from_model_ignored")
    assert alarm["value"] == "9.00"


def test_resolve_doc_fact_does_not_see_group_documents(tmp_path, monkeypatch):
    """Периметр адресного резолва тот же, что у общего прохода: числа
    материнской компании носят те же названия и больше на порядок."""
    dossier = {
        "account_id": "ACC-1",
        "scenario_id": "S1",
        "docs": [
            {
                "file": "own.pdf",
                "doc_type": "agreement",
                "date": "2025-01-01",
                "scope": "borrower",
                "text": "обязательство заёмщика 100.00",
            },
            {
                "file": "group.pdf",
                "doc_type": "audit_report",
                "date": "2025-12-31",
                "scope": "group",
                "text": "обязательство Группы 999999.00",
            },
        ],
        "docs_rejected": [],
        "quarantined": [],
    }
    seen = {}

    def fake_call(prompt, schema, schema_version, **kw):
        seen["prompt"] = prompt
        return {"found": True, "value": "999999.00", "quote": "обязательство Группы 999999.00"}

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    got = facts_extract.resolve_doc_fact(tmp_path, dossier, "severance_liability", "обязательство")
    assert "999999.00" not in seen["prompt"]  # групповой текст не попал в промпт
    assert got is None  # и цитата из него не верифицируется корпусом заёмщика


def test_facts_not_cached_when_dossier_degraded(tmp_path, monkeypatch):
    """Досье при транзиентном сбое не ложится на диск, но объект отдаётся
    дальше. Факты по неполному набору документов закреплялись под
    FACTS_VERSION без единого своего алярма и переживали устранение причины —
    поймано вживую на прогоне (ревью PR #23, вторая волна)."""
    degraded = {
        **DOSSIER,
        "alarms": [{"kind": "group_routing_failed", "file": "x.pdf", "error": "CassetteMiss"}],
    }
    monkeypatch.setattr(facts_extract.llm, "call", facts_only(lambda *a, **k: empty()))
    facts_extract.extract_facts(tmp_path, degraded)
    assert not (tmp_path / "facts" / "ACC-1.json").exists()

    # Причина устранена — факты собираются и кэшируются как обычно.
    facts_extract.extract_facts(tmp_path, DOSSIER)
    assert (tmp_path / "facts" / "ACC-1.json").exists()


def test_issuer_failure_blocks_facts_cache(tmp_path, monkeypatch):
    """Отказ издателя — деградация досье, а значит и фактов. Без этого факты без
    группового документа закэшировались бы и пережили починку маршрутизации:
    досье пересобралось бы, а extract_facts вернул бы старый артефакт, и ячейка
    осталась бы на лестнице навсегда (ревью PR #23, четвёртая волна)."""
    degraded = {
        **DOSSIER,
        "alarms": [{"kind": "issuer_extraction_failed", "file": "group.pdf"}],
    }
    monkeypatch.setattr(facts_extract.llm, "call", facts_only(lambda *a, **k: empty()))
    facts_extract.extract_facts(tmp_path, degraded)
    assert not (tmp_path / "facts" / "ACC-1.json").exists()


def test_degraded_kinds_have_single_source():
    """Два списка одного набора уже разъезжались дважды. Стадия фактов обязана
    читать набор досье, а не держать свою копию."""
    import dossier

    assert "issuer_extraction_failed" in dossier.DEGRADED_KINDS
    assert dossier.ROUTING_DEGRADED <= dossier.DEGRADED_KINDS


def test_group_capex_scale_applied(tmp_path, monkeypatch):
    """Суммы «в тысячах» без дробной части умножаются кодом: числитель приезжает
    из чужой отчётности, знаменатель нормализован построчно (ревью PR #23)."""
    text = (
        "Note 7. There were no disposals of property, plant and equipment during the year. "
        "All amounts in thousands of United States dollars. "
        "Net book value at the beginning of the year 148,029 "
        "Depreciation charge for the year 15,826 "
        "Net book value at the end of the year 154,050"
    )
    raw = ppe(
        opening_value="148029",
        opening_quote="Net book value at the beginning of the year 148,029",
        closing_value="154050",
        closing_quote="Net book value at the end of the year 154,050",
        depreciation="15826",
        depreciation_quote="Depreciation charge for the year 15,826",
        amount_scale="1000",
        units_quote="All amounts in thousands of United States dollars",
    )
    dossier = {**GROUP_DOSSIER, "docs": [{**GROUP_DOSSIER["docs"][0], "text": text}]}
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(raw))
    facts = facts_extract.extract_facts(tmp_path, dossier)
    assert facts["doc_facts"][facts_extract.GROUP_CAPEX_KEY] == "21847000"


def test_group_capex_scale_ignored_for_cent_precision(tmp_path, monkeypatch):
    """Шапка «in thousands» относится к таблицам отчётности, а примечание рядом
    печатает полные суммы — ровно так устроен документ публичного набора. Сумма
    с точностью до цента не бывает «в тысячах»."""
    text = GROUP_TEXT + " All amounts in thousands of United States dollars"
    raw = ppe(amount_scale="1000", units_quote="All amounts in thousands of United States dollars")
    dossier = {**GROUP_DOSSIER, "docs": [{**GROUP_DOSSIER["docs"][0], "text": text}]}
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(raw))
    facts = facts_extract.extract_facts(tmp_path, dossier)
    assert facts["doc_facts"][facts_extract.GROUP_CAPEX_KEY] == "21847362.55"
    assert any(a["kind"] == "group_capex_scale_ignored" for a in facts["alarms"])


def test_group_capex_foreign_currency_refused(tmp_path, monkeypatch):
    """Пересчитать нечем: курс материнской компании к строкам заёмщика
    отношения не имеет, а молча принять чужую валюту хуже отсутствия ответа."""
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(ppe(currency="EUR")))
    facts = facts_extract.extract_facts(tmp_path, GROUP_DOSSIER)
    assert facts_extract.GROUP_CAPEX_KEY not in facts["doc_facts"]
    assert any(a["kind"] == "group_capex_foreign_currency" for a in facts["alarms"])


def test_resolve_doc_fact_not_cached_when_dossier_degraded(tmp_path, monkeypatch):
    """«Числа нет» по неполному досье приходит БЕЗ алярма (found=false — законный
    ответ) и пережило бы устранение причины (ревью PR #23, пятая волна)."""
    degraded = {
        **DOSSIER,
        "alarms": [{"kind": "routing_failed", "file": "x.pdf", "error": "boom"}],
    }
    monkeypatch.setattr(facts_extract.llm, "call", lambda *a, **k: {"found": False, "value": "", "quote": ""})
    facts_extract.resolve_doc_fact(tmp_path, degraded, "severance_liability", "обязательство")
    assert not (tmp_path / "facts" / "ACC-1.doc.severance_liability.json").exists()


def test_group_capex_scale_applied_for_millions(tmp_path, monkeypatch):
    """Отчётность «в миллионах» с одной цифрой после запятой — типовая форма, а
    не спор с шапкой: масштаб применяется как названо. Отказ здесь снимал бы
    ключ при полностью подтверждённых данных (ревью PR #23, седьмая волна)."""
    text = (
        "Note 7. There were no disposals of property, plant and equipment during the year. "
        "All amounts in millions of United States dollars. "
        "Net book value at the beginning of the year 148.0 "
        "Depreciation charge for the year 15.8 "
        "Net book value at the end of the year 154.1"
    )
    raw = ppe(
        opening_value="148.0",
        opening_quote="Net book value at the beginning of the year 148.0",
        closing_value="154.1",
        closing_quote="Net book value at the end of the year 154.1",
        depreciation="15.8",
        depreciation_quote="Depreciation charge for the year 15.8",
        amount_scale="1000000",
        units_quote="All amounts in millions of United States dollars",
    )
    dossier = {**GROUP_DOSSIER, "docs": [{**GROUP_DOSSIER["docs"][0], "text": text}]}
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(raw))
    facts = facts_extract.extract_facts(tmp_path, dossier)
    assert facts["doc_facts"][facts_extract.GROUP_CAPEX_KEY] == "21900000"


def test_scale_decision_survives_thousand_separators():
    """Дробность меряется тем же нормализатором, что и сами суммы: иначе исход
    зависел бы от того, поставила ли модель разделители вопреки промпту."""
    assert facts_extract._fraction_digits("154,050.10") == 2
    assert facts_extract._fraction_digits("154050.10") == 2
    assert facts_extract._fraction_digits("154050") == 0
    assert facts_extract._fraction_digits("не число") is None


def test_group_capex_zero_refused(tmp_path, monkeypatch):
    """Ноль — типовой дефолт непонятого поля, и названным числом он минует оба
    условия применимости. На max-ковенанте это гарантированный COMPLIANT."""
    text = GROUP_TEXT + " Additions during the year 0"
    raw = ppe(additions="0", additions_quote="Additions during the year 0", no_disposals=False)
    dossier = {**GROUP_DOSSIER, "docs": [{**GROUP_DOSSIER["docs"][0], "text": text}]}
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(raw))
    facts = facts_extract.extract_facts(tmp_path, dossier)
    assert facts_extract.GROUP_CAPEX_KEY not in facts["doc_facts"]
    assert any(a["kind"] == "group_capex_non_positive" for a in facts["alarms"])


def test_group_capex_conflict_compares_values_not_spellings(tmp_path, monkeypatch):
    """Умножение на масштаб сдвигает экспоненту, и одно и то же число получает
    две записи. Сравнение по строке дало бы ложный конфликт и сняло бы ключ
    (ревью PR #23, седьмая волна)."""
    scaled_text = (
        "Note 7. There were no disposals of property, plant and equipment during the year. "
        "All amounts in thousands of United States dollars. "
        "Net book value at the beginning of the year 148,029 "
        "Depreciation charge for the year 15,826 "
        "Net book value at the end of the year 154,050"
    )
    dossier = {
        **GROUP_DOSSIER,
        "docs": [
            {**GROUP_DOSSIER["docs"][0], "file": "a-scaled.pdf", "text": scaled_text},
            {**GROUP_DOSSIER["docs"][0], "file": "b-plain.pdf", "text": GROUP_TEXT},
        ],
    }

    def fake_call(prompt, schema, schema_version, **kw):
        if "in thousands" in prompt:
            return ppe(
                opening_value="148029",
                opening_quote="Net book value at the beginning of the year 148,029",
                closing_value="154050",
                closing_quote="Net book value at the end of the year 154,050",
                depreciation="15826",
                depreciation_quote="Depreciation charge for the year 15,826",
                amount_scale="1000",
                units_quote="All amounts in thousands of United States dollars",
            )
        return ppe(
            opening_value="148029000",
            opening_quote="Net book value at the beginning of the year $148,028,989.69",
            closing_value="154050000",
            closing_quote="Net book value at the end of the year $154,050,122.81",
            depreciation="15826000",
            depreciation_quote="Depreciation charge for the year $15,826,229.43",
        )

    monkeypatch.setattr(facts_extract.llm, "call", fake_call)
    facts = facts_extract.extract_facts(tmp_path, dossier)
    assert not any(a["kind"] == "group_capex_conflict" for a in facts["alarms"])
    assert facts["doc_facts"][facts_extract.GROUP_CAPEX_KEY] == "21847000"


def test_group_capex_unnamed_currency_is_alarmed_not_silent(tmp_path, monkeypatch):
    """Пустая валюта — штатный ответ по промпту, и она проваливалась мимо гейта:
    расчёт шёл в валюте расчёта по умолчанию, без следа. Считаем по-прежнему
    (отказ стоил бы ячейки уже сегодня), но допущение видно (ревью PR #23)."""
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(ppe(currency="")))
    facts = facts_extract.extract_facts(tmp_path, GROUP_DOSSIER)
    assert facts["doc_facts"][facts_extract.GROUP_CAPEX_KEY] == "21847362.55"
    assert any(a["kind"] == "group_capex_currency_unnamed" for a in facts["alarms"])


def test_unparsed_amount_does_not_kill_the_whole_calculation(tmp_path, monkeypatch):
    """Одиночная запятая ровно с тремя цифрами намеренно не снимается
    _normalize_limit. Такое `additions` роняло весь расчёт, хотя остальные три
    числа целы и тождество отработало бы (ревью PR #23, девятая волна)."""
    text = GROUP_TEXT + " Additions during the year 154,050"
    raw = ppe(
        additions="154,050",
        additions_quote="Additions during the year 154,050",
        amount_scale="1000",
        units_quote="Depreciation charge for the year $15,826,229.43",
    )
    dossier = {**GROUP_DOSSIER, "docs": [{**GROUP_DOSSIER["docs"][0], "text": text}]}
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(raw))
    facts = facts_extract.extract_facts(tmp_path, dossier)
    # additions отброшено number()-ом как непарсибельное, расчёт ушёл на
    # тождество по трём оставшимся числам; масштаб не применён — суммы с центами.
    assert facts["doc_facts"][facts_extract.GROUP_CAPEX_KEY] == "21847362.55"
    assert any(
        a["kind"] == "invalid_number" and a["field"] == "group_capex_scale_decision" for a in facts["alarms"]
    )


def test_group_capex_unnamed_scale_is_alarmed(tmp_path, monkeypatch):
    """Пустой масштаб — тоже допущение по умолчанию, и цена промаха выше, чем у
    валюты: «in thousands» в шапке занижает числитель в 10³. На публичном наборе
    модель возвращает его пустым, то есть путь живой (ревью PR #23, 10-я волна)."""
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(ppe(amount_scale="")))
    facts = facts_extract.extract_facts(tmp_path, GROUP_DOSSIER)
    assert facts["doc_facts"][facts_extract.GROUP_CAPEX_KEY] == "21847362.55"
    assert any(a["kind"] == "group_capex_scale_unnamed" for a in facts["alarms"])


def test_group_capex_incomplete_movement_is_named(tmp_path, monkeypatch):
    """Пустые числа примечания давали отказ без следа: в run-report было видно
    только group_doc_attached, то есть «документ привязан и прочитан». Теперь
    видно, что он привязан и НЕ прочитан (ревью PR #23, 11-я волна)."""
    monkeypatch.setattr(facts_extract.llm, "call", _group_dispatch(ppe(opening_value="", opening_quote="")))
    facts = facts_extract.extract_facts(tmp_path, GROUP_DOSSIER)
    assert facts_extract.GROUP_CAPEX_KEY not in facts["doc_facts"]
    alarm = next(a for a in facts["alarms"] if a["kind"] == "group_capex_movement_incomplete")
    assert alarm["fields"] == ["opening"]


# --- определение EBITDA из договора ------------------------------------------

_EBITDA_DEF_NARROW = "EBITDA означает Выручку за вычетом Операционных расходов, как раскрыто в примечаниях."
_EBITDA_DEF_BROAD = "EBITDA означает выручку за вычетом всех операционных расходов за период."


def _agreement_dossier(definition: str) -> dict:
    return {
        "account_id": "ACC-1",
        "scenario_id": "S1",
        "docs": [
            {
                "file": "agreement.pdf",
                "doc_type": "agreement",
                "date": "2025-01-01",
                "text": f"Кредитный договор. {definition} Прочие условия.",
            }
        ],
        "docs_rejected": [],
        "quarantined": [],
        "alarms": [],
    }


def _def_call(found: bool, quote: str):
    def call(prompt, schema, schema_version, **kw):
        assert schema_version == facts_extract.EBITDA_DEF_SCHEMA_VERSION
        return {"found": found, "quote": quote}

    return call


def test_ebitda_definition_classified_by_code():
    """Классифицирует КОД по цитате, модель только находит определение: статья
    без квантора — line_item, квантор всеобщности — all_opex, определение
    другой природы или не про метрику — None."""
    assert facts_extract._classify_ebitda_quote(_EBITDA_DEF_NARROW) == "line_item"
    assert facts_extract._classify_ebitda_quote(_EBITDA_DEF_BROAD) == "all_opex"
    assert (
        facts_extract._classify_ebitda_quote("EBITDA means Revenue less total operating costs") == "all_opex"
    )
    assert facts_extract._classify_ebitda_quote("EBITDA means profit before tax and depreciation") is None
    assert facts_extract._classify_ebitda_quote("операционные расходы без определения метрики") is None


def test_quote_requires_addback_needs_both_onetime_and_adjustment_words():
    """Признак ортогонален выбору роллапа опекса (задача 3): договор вправе
    сузить статью И потребовать разовую корректировку одновременно. Одного
    слова о «разовости» недостаточно — оно встречается и по другим поводам,
    поэтому нужно СОЧЕТАНИЕ со словом о самой корректировке/добавлении."""
    assert facts_extract._quote_requires_addback(
        "EBITDA рассчитывается как Выручка за вычетом Операционных расходов "
        "с учётом разовых корректировок, согласованных аудитором."
    )
    assert facts_extract._quote_requires_addback(
        "EBITDA — Выручку за вычетом Операционных расходов, увеличенную на "
        "любые разовые статьи, добавленные аудиторами Заёмщика обратно к EBITDA."
    )
    assert facts_extract._quote_requires_addback(
        "EBITDA means Revenue less Operating Expenses, adjusted for any "
        "one-time addback items agreed by the auditors."
    )
    # Только «разовость» без слова о корректировке/добавлении — не сигнал.
    assert not facts_extract._quote_requires_addback(
        "разовые платежи по договору не включаются в состав операционных расходов"
    )
    # Только слово о корректировке без «разовости» — тоже не сигнал: узкое
    # прочтение опекса (_EBITDA_DEF_NARROW) само по себе ничего не требует.
    assert not facts_extract._quote_requires_addback(_EBITDA_DEF_NARROW)
    assert not facts_extract._quote_requires_addback(_EBITDA_DEF_BROAD)


def test_ebitda_definition_line_item(tmp_path, monkeypatch):
    monkeypatch.setattr(facts_extract.llm, "call", _def_call(True, _EBITDA_DEF_NARROW))
    got = facts_extract.ebitda_definition(tmp_path, _agreement_dossier(_EBITDA_DEF_NARROW))
    assert got == {"reading": "line_item", "quote": _EBITDA_DEF_NARROW, "needs_addback": False}


def test_ebitda_definition_broad(tmp_path, monkeypatch):
    monkeypatch.setattr(facts_extract.llm, "call", _def_call(True, _EBITDA_DEF_BROAD))
    got = facts_extract.ebitda_definition(tmp_path, _agreement_dossier(_EBITDA_DEF_BROAD))
    assert got is not None and got["reading"] == "all_opex" and got["needs_addback"] is False


def test_ebitda_definition_with_addback_clause(tmp_path, monkeypatch):
    """Определение и про статью опекса (line_item), и про разовую
    корректировку одновременно — оба признака независимы (кейс S3 6.1)."""
    quote = (
        "EBITDA рассчитывается как Выручка за вычетом Операционных расходов "
        "с учётом разовых корректировок, согласованных аудитором."
    )
    monkeypatch.setattr(facts_extract.llm, "call", _def_call(True, quote))
    got = facts_extract.ebitda_definition(tmp_path, _agreement_dossier(quote))
    assert got == {"reading": "line_item", "quote": quote, "needs_addback": True}


def test_ebitda_definition_unverified_quote_is_dropped(tmp_path, monkeypatch):
    # Цитата не из договора — факта нет (контракт guard, как у всех потребителей).
    monkeypatch.setattr(
        facts_extract.llm, "call", _def_call(True, "EBITDA означает что-то выдуманное про операционные")
    )
    assert facts_extract.ebitda_definition(tmp_path, _agreement_dossier(_EBITDA_DEF_NARROW)) is None


def test_ebitda_definition_without_agreement_is_none(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("вызова быть не должно: договора в досье нет")

    monkeypatch.setattr(facts_extract.llm, "call", boom)
    assert facts_extract.ebitda_definition(tmp_path, DOSSIER) is None


def test_ebitda_definition_schema_failure_not_cached(tmp_path, monkeypatch):
    def rejected(prompt, schema, schema_version, **kw):
        raise facts_extract.llm.SchemaRejected("bad")

    monkeypatch.setattr(facts_extract.llm, "call", rejected)
    dossier = _agreement_dossier(_EBITDA_DEF_NARROW)
    assert facts_extract.ebitda_definition(tmp_path, dossier) is None
    # Отказ не закреплён артефактом: на повторе (после устранения причины)
    # вызов уйдёт заново, а не вернёт found=false с диска.
    assert not (tmp_path / "facts" / "ACC-1.ebitda_def.json").exists()
    monkeypatch.setattr(facts_extract.llm, "call", _def_call(True, _EBITDA_DEF_NARROW))
    assert facts_extract.ebitda_definition(tmp_path, dossier) is not None


# --- resolve_doc_metric -------------------------------------------------------


_METRIC_DOSSIER = {"account_id": "ACC-M", "alarms": [], "docs": []}


def _metric_answer(computable=True, expression="agg(FINANCING, out)"):
    def call(prompt, schema, version, max_tokens=0):
        return {"computable": computable, "expression": expression}

    return call


def test_resolve_doc_metric_valid_expression(tmp_path, monkeypatch):
    """Формульный резолв: LLM выписывает агрегат, грамматика валидирует,
    выражение возвращается текстом — считать его будет код."""
    monkeypatch.setattr(facts_extract.llm, "call", _metric_answer())
    expr = facts_extract.resolve_doc_metric(tmp_path, _METRIC_DOSSIER, "principal_payments", "цитата")
    assert expr == "agg(FINANCING, out)"


def test_resolve_doc_metric_rejects_constants_doc_and_garbage(tmp_path, monkeypatch):
    # Const запрещён конструкцией — эхо порога здесь невозможно синтаксически;
    # doc() запрещён — резолв не ссылается на другие нерешённые ключи; мусор
    # и чужая категория валятся на грамматике/таксономии.
    for i, bad in enumerate(
        (
            "add(agg(FINANCING, out), const(250000))",
            "ratio(doc(other_key), agg(REVENUE, in))",
            "const(3.5)",
            "не формула вовсе",
            "agg(NOT_A_CATEGORY, out)",
        )
    ):
        monkeypatch.setattr(facts_extract.llm, "call", _metric_answer(expression=bad))
        assert facts_extract.resolve_doc_metric(tmp_path, _METRIC_DOSSIER, f"key_{i}", "ц") is None


def test_resolve_doc_metric_not_computable_is_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(facts_extract.llm, "call", _metric_answer(computable=False, expression=""))
    assert facts_extract.resolve_doc_metric(tmp_path, _METRIC_DOSSIER, "external_index", "ц") is None


def test_resolve_doc_metric_accepts_counterparty_named_set(tmp_path, monkeypatch):
    """Фильтр по контрагентам — именованным множеством: механизм уже есть в
    грамматике DSL, резолву формул не хватало только строчки в промпте."""
    expr = "agg(ALL, out, counterparty_in(unrestricted_subsidiaries))"
    monkeypatch.setattr(facts_extract.llm, "call", _metric_answer(expression=expr))
    assert facts_extract.resolve_doc_metric(tmp_path, _METRIC_DOSSIER, "asset_transfer", "ц") == expr


def test_resolve_doc_metric_rejects_literal_counterparty_names(tmp_path, monkeypatch):
    """Литеральный список контрагентов в этом резолве запрещён — модель не
    имеет права выдумывать имена, только ссылаться на именованные множества."""
    for bad in (
        "agg(ALL, out, counterparty_in(['ООО Ромашка']))",
        "agg(ALL, out, counterparty_in('ООО Ромашка'))",
    ):
        monkeypatch.setattr(facts_extract.llm, "call", _metric_answer(expression=bad))
        assert facts_extract.resolve_doc_metric(tmp_path, _METRIC_DOSSIER, "asset_transfer", "ц") is None
