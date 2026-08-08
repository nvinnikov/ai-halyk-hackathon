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
    assert got == {"value": "9450000.00", "quote": "консолидированный CapEx $9,450,000.00"}


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
    assert got == {"value": "-9450000.00", "quote": "консолидированный CapEx $9,450,000.00"}


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
        "shares": [{"name": n, "share_percent": s, "quote": q} for n, s, q in shares],
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
    assert any(a["kind"] == "group_capex_negative" for a in facts["alarms"])


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
    assert any(a["kind"] == "group_capex_negative" for a in facts["alarms"])


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
