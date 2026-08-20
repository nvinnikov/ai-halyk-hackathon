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


def test_limit_with_thousands_separator_in_quote_is_valid(tmp_path, monkeypatch):
    # Модель почти всегда цитирует порог с разделителями тысяч ($1,500,000.00),
    # а limit отдаёт голым числом (1500000.00) — это форматирование, не признак
    # того, что порога нет в цитате.
    cov = covenant(
        limit="1500000.00",
        quote="Пункт 6.1: капитальные затраты не должны превышать $1,500,000.00",
    )
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(cov["quote"]), set())
    sp = art["clauses"]["6.1"]
    assert sp["valid"] is True and sp["errors"] == []


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


def test_title_key_normalizes_heading_across_formatting(tmp_path, monkeypatch):
    # Два пункта, сформулированных по-разному (регистр, пунктуация, номер),
    # но с одинаковым заголовком обязательства — ключ должен совпасть: по нему
    # (не по DSL-сигнатуре) идёт основной матч с шаблоном в solve (задача 24).
    quote_a = "Пункт 6.1: капитальные затраты Группы к EBITDA"
    quote_b = "Пункт 7.1. капитальные ЗАТРАТЫ группы к ebitda!"
    monkeypatch.setattr(
        specs_extract.llm, "call", lambda *a, **k: {"covenants": [covenant(clause="6.1", quote=quote_a)]}
    )
    art_a = specs_extract.extract_specs(tmp_path / "a", make_dossier(quote_a), set())
    monkeypatch.setattr(
        specs_extract.llm, "call", lambda *a, **k: {"covenants": [covenant(clause="7.1", quote=quote_b)]}
    )
    art_b = specs_extract.extract_specs(tmp_path / "b", make_dossier(quote_b), set())
    key_a = art_a["clauses"]["6.1"]["title_key"]
    key_b = art_b["clauses"]["7.1"]["title_key"]
    assert key_a and key_a == key_b


def test_title_key_taken_from_document_heading_not_quote(tmp_path, monkeypatch):
    """Ревью PR #9 🟡: модель цитирует ТЕЛО пункта, заголовок остаётся в
    документе перед телом — ключ обязан браться из текста договора по номеру
    пункта (0/36 совпадений по цитате против 36/36 по заголовку на замере)."""
    body = "Заёмщик обязуется не превышать лимит капитальных затрат."
    doc_text = f"Пункт 6.1 Максимальная капитальная интенсивность. {body}"
    monkeypatch.setattr(
        specs_extract.llm, "call", lambda *a, **k: {"covenants": [covenant(clause="6.1", quote=body)]}
    )
    art = specs_extract.extract_specs(tmp_path, make_dossier(doc_text), set())
    from templates import title_key

    assert art["clauses"]["6.1"]["title_key"] == title_key("Максимальная капитальная интенсивность")


def test_title_key_falls_back_to_quote_when_heading_absent(tmp_path, monkeypatch):
    """Другая вёрстка договора (нет строки «Пункт N.M Заголовок.») — ключ
    откатывается на прежний вариант из цитаты, матч закроет сигнатура."""
    quote = "Заёмщик обязуется поддерживать коэффициент."
    monkeypatch.setattr(
        specs_extract.llm, "call", lambda *a, **k: {"covenants": [covenant(clause="6.1", quote=quote)]}
    )
    art = specs_extract.extract_specs(tmp_path, make_dossier(quote), set())
    from templates import title_key

    assert art["clauses"]["6.1"]["title_key"] == title_key(quote)


def test_limit_forms_decimal_comma_and_spaced_percent():
    """Ревью PR #9 (5-я волна): «1,44» (десятичная запятая) и «4 %» (пробел
    перед процентом) — легитимные вёрстки порога в цитате."""
    assert specs_extract._limit_in_quote("1.44", "не ниже 1,44х по итогам года")
    assert specs_extract._limit_in_quote("0.04", "не более 4 % от выручки")
    assert not specs_extract._limit_in_quote("1.44", "не ниже 2,00х по итогам года")


def test_limit_scaled_form_in_quote():
    """Ревью PR #9 (6-я волна): «10 млн» / «1.5 million» — масштабная вёрстка
    порога не должна стоить ячейку; несовпавший масштаб — по-прежнему провал."""
    assert specs_extract._limit_in_quote("10000000", "не более 10 млн долларов")
    assert specs_extract._limit_in_quote("1500000", "not exceeding 1.5 million")
    assert not specs_extract._limit_in_quote("10000000", "не более 20 млн долларов")


def test_limit_percent_word_form_in_quote():
    """Ревью PR #9 (27-я волна): словесная форма процента — «7 (семи)
    процентов» / "7 percent" — без знака %; чужое число или не-процент
    по-прежнему провал."""
    assert specs_extract._limit_in_quote("0.07", "не более 7 (семи) процентов от выручки")
    assert specs_extract._limit_in_quote("0.07", "not exceeding 7 percent of revenue")
    assert specs_extract._limit_in_quote("0.045", "не выше 4,5 процента годовых")
    assert not specs_extract._limit_in_quote("0.07", "не более 7 штук")
    assert not specs_extract._limit_in_quote("0.07", "не более 17 процентов")


def test_limit_currency_form_normalized_to_number(tmp_path, monkeypatch):
    # Живой паттерн Gemini (task-28, третий паттерн): limit приходит как
    # '$7,500,000.00' — Decimal падал («limit: не число»), спека invalid,
    # ячейка уезжала на лестницу при полностью здоровой цитате и метрике.
    # Валютный знак, пробелы и разделители тысяч снимаются до проверки.
    cov = covenant(limit="$7,500,000.00", quote="Пункт 6.1 не более $7,500,000.00 в год")
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(cov["quote"]), set())
    sp = art["clauses"]["6.1"]
    assert sp["valid"] is True, sp["errors"]
    assert sp["limit"] == "7500000.00"


def test_limit_multiplier_suffix_normalized(tmp_path, monkeypatch):
    # «2.5x» из инструкции промпта модель иногда возвращает буквально,
    # с суффиксом кратности (латинским или кириллическим «х»). Метрика —
    # отношение: кратность в тексте порога согласована с формой метрики
    # (правило формы порога), иначе тест ловил бы limit_shape_mismatch,
    # а не то, что он реально проверяет — нормализацию суффикса.
    cov = covenant(
        limit="2.5x",
        quote="Пункт 6.1 не более 2.5x показателя",
        metric="ratio(agg(CAPEX, out), agg(REVENUE, in))",
    )
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(cov["quote"]), set())
    sp = art["clauses"]["6.1"]
    assert sp["valid"] is True, sp["errors"]
    assert sp["limit"] == "2.5"


def test_limit_foreign_currency_sign_stays_loud(tmp_path, monkeypatch):
    # Ревью PR #11, раунд 2: метрика к сравнению уже нормализована в USD,
    # и знак чужой валюты в пороге — единственный сигнал, что порог не в
    # базовой валюте. Молча снять его = уверенно неверный вердикт с ошибкой
    # в сотни раз (status обнуляет ячейку). Снимается безусловно только $;
    # порог с €/£/₸ остаётся непарсибельным и громко падает в invalid_spec.
    for sign in ("€", "£", "₸"):
        raw = f"{sign}1,234,567"
        cov = covenant(limit=raw, quote=f"Пункт 6.1 не более {raw} в год")
        monkeypatch.setattr(specs_extract.llm, "call", lambda *a, _cov=cov, **k: {"covenants": [_cov]})
        art = specs_extract.extract_specs(tmp_path / sign, make_dossier(cov["quote"]), set())
        sp = art["clauses"]["6.1"]
        assert sp["valid"] is False, sign
        assert any("не число" in e for e in sp["errors"]), (sign, sp["errors"])


def test_limit_ambiguous_single_comma_three_digits_stays_loud(tmp_path, monkeypatch):
    # Ревью PR #11: '0,075' (доля с десятичной запятой) и '1,125' (кратность)
    # неотличимы от разрядной запятой '7,500' — снимать её молча нельзя:
    # '0,075' → '0075' == 75 завышал бы порог в 1000 раз, а _limit_in_quote
    # это не ловит (цитата калечится тем же _degroup_thousands). Неоднозначная
    # форма остаётся как есть и громко падает в invalid_spec, как до правки.
    for ambiguous in ("0,075", "1,125", "7,500"):
        cov = covenant(limit=ambiguous, quote=f"Пункт 6.1 не более {ambiguous} от выручки")
        monkeypatch.setattr(specs_extract.llm, "call", lambda *a, _cov=cov, **k: {"covenants": [_cov]})
        art = specs_extract.extract_specs(tmp_path / ambiguous, make_dossier(cov["quote"]), set())
        sp = art["clauses"]["6.1"]
        assert sp["valid"] is False, ambiguous
        assert any("limit" in e for e in sp["errors"]), (ambiguous, sp["errors"])


def test_limit_unambiguous_groupings_normalized(tmp_path, monkeypatch):
    # Две и более запятых или запятая при точке-десятичной — однозначно
    # разрядные, снимаются; десятичная запятая с 1–2 знаками — в точку.
    cases = {"7,500,000": "7500000", "$1,234,567.89": "1234567.89", "1,44": "1.44"}
    for raw, expected in cases.items():
        assert specs_extract._normalize_limit(raw) == expected, raw


def test_non_numeric_limit_invalid_in_check(tmp_path, monkeypatch):
    """«5%» вместо числа — спека невалидна уже в _check с внятной ошибкой,
    а не молча на лестнице после Decimal() в solve."""
    quote = "Пункт 6.1: доля не выше 5% от выручки"
    monkeypatch.setattr(
        specs_extract.llm,
        "call",
        lambda *a, **k: {"covenants": [covenant(clause="6.1", quote=quote, limit="5%")]},
    )
    art = specs_extract.extract_specs(tmp_path, make_dossier(quote), set())
    sp = art["clauses"]["6.1"]
    assert sp["valid"] is False
    assert any("не число" in e for e in sp["errors"])


def test_trigger_doc_key_missing_soft_discards_trigger(tmp_path, monkeypatch):
    """Ревью PR #9 (8-я волна): doc()-ключ в триггере без факта — триггер мягко
    отброшен (не KeyError в evaluate), ключ попадает в missing_doc_keys для
    резолва, спека остаётся валидной."""
    monkeypatch.setattr(
        specs_extract.llm,
        "call",
        lambda *a, **k: {
            "covenants": [
                covenant(
                    metric="agg(CAPEX, out)",
                    trigger="gt(doc(financing_threshold), const(1))",
                )
            ]
        },
    )
    art = specs_extract.extract_specs(tmp_path, make_dossier(covenant()["quote"]), set())
    sp = art["clauses"]["6.1"]
    assert sp["valid"] is True
    assert sp["trigger"] is None
    assert "financing_threshold" in sp["missing_doc_keys"]
    assert any(a["kind"] == "trigger_discarded" for a in art["alarms"])


def test_trigger_doc_key_present_keeps_trigger(tmp_path, monkeypatch):
    monkeypatch.setattr(
        specs_extract.llm,
        "call",
        lambda *a, **k: {
            "covenants": [
                covenant(
                    metric="agg(CAPEX, out)",
                    trigger="gt(doc(financing_threshold), const(1))",
                )
            ]
        },
    )
    art = specs_extract.extract_specs(tmp_path, make_dossier(covenant()["quote"]), {"financing_threshold"})
    sp = art["clauses"]["6.1"]
    assert sp["valid"] is True and sp["trigger"] is not None


def test_limit_in_clause_span_definition_outside_quote():
    """Боевой прогон 2026-08-09: «...не допускать превышения Разрешённой
    величины», а «Разрешённая величина означает 5 процентов...» стоит в том же
    пункте двумя предложениями ниже цитаты. Порог обязан быть напечатан в
    теле ТОГО ЖЕ пункта; чужой пункт — по-прежнему провал."""
    text = (
        "Пункт 6.2 Прочее. Не более 5 процентов чего-то другого. "
        "Пункт 6.3 Максимальные расходы. Заёмщик обязуется не допускать превышения "
        "Разрешённой величины. Разрешённая величина означает 5 процентов "
        "Консолидированных капитальных затрат Группы. "
        "Статья 7 — Ограничительные обязательства. Прочий текст."
    )
    assert specs_extract._limit_in_clause_span("0.05", text, "6.3")
    # В пункте 6.1 порога нет вовсе — провал.
    assert not specs_extract._limit_in_clause_span(
        "0.05", "Пункт 6.1 Иное. Текст без числа. Пункт 6.2 Х.", "6.1"
    )
    # Заголовок пункта не найден — провал, не скан всего договора.
    assert not specs_extract._limit_in_clause_span("0.05", text, "9.9")


def test_check_accepts_limit_defined_in_clause_body():
    """_check: порог вне цитаты, но в теле пункта — спека валидна, путь помечен
    limit_matched_in_clause; порога нет и в теле пункта — прежний
    limit_not_in_quote."""
    quote = "Заёмщик обязуется не допускать превышения Разрешённой величины."
    text = (
        "Пункт 6.3 Максимальные расходы. " + quote + " Разрешённая величина означает "
        "5 процентов Консолидированных капитальных затрат Группы. Статья 7 — Прочее."
    )
    sp = covenant(clause="6.3", quote=quote, limit="0.05", metric="agg(RENT, out)", direction="max")
    checked, _ = specs_extract._check(sp, set(), text)
    assert checked["valid"] and checked.get("limit_matched_in_clause") is True
    sp2 = covenant(clause="6.3", quote=quote, limit="0.09", metric="agg(RENT, out)", direction="max")
    checked2, _ = specs_extract._check(sp2, set(), text)
    assert not checked2["valid"] and "limit_not_in_quote" in checked2["errors"]


# Форма порога определяет форму метрики (правило команды со 2-го места):
# лимит-коэффициент => метрика обязана быть отношением, лимит в валюте =>
# метрика обязана быть суммой. Форма порога читается из текста цитаты, а не
# из его величины — в отличие от solve._family_mismatch, который смотрит на
# разрыв между посчитанным значением и порогом и ловит другой квадрант.


def test_shape_mismatch_multiplier_threshold_vs_absolute_metric(tmp_path, monkeypatch):
    """Квадрант-конфликт 1: суффикс кратности в тексте порога («3.5x»), а
    метрика — абсолютная сумма, не отношение."""
    quote = "Пункт 6.1: капитальные затраты не должны превышать 3.5x показателя"
    cov = covenant(clause="6.1", metric="agg(CAPEX, out)", limit="3.5", quote=quote)
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(quote), set())
    sp = art["clauses"]["6.1"]
    assert sp["valid"] is False
    assert "limit_shape_mismatch" in sp["errors"]
    alarms = [a for a in art["alarms"] if a["kind"] == "limit_shape_mismatch"]
    assert len(alarms) == 1
    assert alarms[0]["threshold_shape"] == "ratio"
    assert alarms[0]["metric_shape"] == "absolute"


def test_shape_mismatch_currency_threshold_vs_ratio_metric(tmp_path, monkeypatch):
    """Квадрант-конфликт 2: знак валюты в тексте порога, а метрика — отношение,
    не сумма."""
    quote = "Пункт 6.2: показатель не должен превышать $500,000 в квартал"
    cov = covenant(
        clause="6.2", metric="ratio(agg(CAPEX, out), agg(REVENUE, in))", limit="500000", quote=quote
    )
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(quote), set())
    sp = art["clauses"]["6.2"]
    assert sp["valid"] is False
    assert "limit_shape_mismatch" in sp["errors"]
    alarms = [a for a in art["alarms"] if a["kind"] == "limit_shape_mismatch"]
    assert alarms[0]["threshold_shape"] == "absolute"
    assert alarms[0]["metric_shape"] == "ratio"


def test_shape_agrees_multiplier_threshold_with_ratio_metric(tmp_path, monkeypatch):
    """Согласованный квадрант 1 (кратность/отношение) — правило молчит."""
    quote = "Пункт 6.3: коэффициент долга не должен превышать 3.5x показателя"
    cov = covenant(clause="6.3", metric="ratio(agg(CAPEX, out), agg(REVENUE, in))", limit="3.5", quote=quote)
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(quote), set())
    sp = art["clauses"]["6.3"]
    assert sp["valid"] is True, sp["errors"]
    assert not any(a["kind"] == "limit_shape_mismatch" for a in art["alarms"])


def test_shape_agrees_currency_threshold_with_absolute_metric(tmp_path, monkeypatch):
    """Согласованный квадрант 2 (валюта/сумма) — правило молчит."""
    quote = "Пункт 6.4: расходы не должны превышать $500,000 в квартал"
    cov = covenant(clause="6.4", metric="agg(CAPEX, out)", limit="500000", quote=quote)
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(quote), set())
    sp = art["clauses"]["6.4"]
    assert sp["valid"] is True, sp["errors"]
    assert not any(a["kind"] == "limit_shape_mismatch" for a in art["alarms"])


def test_shape_unrecognized_form_has_no_opinion(tmp_path, monkeypatch):
    """Отказ 1 (главный): форма порога в тексте не распознана (голое число
    без знака валюты/кратности/процента) — правило молчит, даже если семьи
    в реальности расходятся."""
    quote = "Пункт 6.5: капитальные затраты не должны превышать 500000"
    cov = covenant(
        clause="6.5", metric="ratio(agg(CAPEX, out), agg(REVENUE, in))", limit="500000", quote=quote
    )
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(quote), set())
    sp = art["clauses"]["6.5"]
    assert sp["valid"] is True, sp["errors"]
    assert not any(a["kind"] == "limit_shape_mismatch" for a in art["alarms"])


def test_shape_check_skipped_when_limit_not_in_quote(tmp_path, monkeypatch):
    """Отказ 2: порог не найден в цитате — уже валит спеку своей проверкой
    (limit_not_in_quote), форма не проверяется повторно."""
    quote = "Пункт 6.6: капитальные затраты не должны превышать установленный предел"
    cov = covenant(clause="6.6", metric="agg(CAPEX, out)", limit="500000", quote=quote)
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(quote), set())
    sp = art["clauses"]["6.6"]
    assert sp["valid"] is False
    assert "limit_not_in_quote" in sp["errors"]
    assert "limit_shape_mismatch" not in sp["errors"]
    assert not any(a["kind"] == "limit_shape_mismatch" for a in art["alarms"])


def test_shape_check_skipped_when_metric_invalid(tmp_path, monkeypatch):
    """Отказ 3: метрика не распарсилась — ранний возврат в _check до формы."""
    quote = "Пункт 6.7: показатель не должен превышать 3.5x показателя"
    cov = covenant(clause="6.7", metric="__import__('os')", limit="3.5", quote=quote)
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(quote), set())
    sp = art["clauses"]["6.7"]
    assert sp["valid"] is False
    assert not any(a["kind"] == "limit_shape_mismatch" for a in art["alarms"])


def test_shape_check_skipped_for_bare_doc_metric(tmp_path, monkeypatch):
    """Отказ 4 (найден замером на приватном наборе): вся метрика — голый
    doc(ключ). family_of видит только AST и для непрозрачного значения,
    посчитанного вне DSL, по умолчанию отдаёт 'absolute' — не потому что
    метрика действительно сумма, а потому что это не Ratio. Боевой кейс:
    доля выручки квартала от годовой выражена doc()-ключом при пороге
    «0.30x» — верная спека ложно конфликтовала бы с формой и уезжала на
    лестницу. ratio(doc(...), ...) под исключение не попадает — там doc()
    лишь аргумент, верхний узел Ratio, family_of классифицирует его верно
    (см. test_shape_agrees_multiplier_threshold_with_ratio_metric)."""
    quote = "Пункт 6.8: доля выручки квартала не должна превышать 0.30x от годовой"
    cov = covenant(clause="6.8", metric="doc(quarterly_revenue_share)", limit="0.30", quote=quote)
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(quote), {"quarterly_revenue_share"})
    sp = art["clauses"]["6.8"]
    assert sp["valid"] is True, sp["errors"]
    assert not any(a["kind"] == "limit_shape_mismatch" for a in art["alarms"])


def test_threshold_form_no_collision_with_neighboring_multiplier(tmp_path, monkeypatch):
    """Ревью раунд 1 (Critical): короткий порог не должен подхватывать форму
    ЧУЖОГО числа по совпадению цифры внутри него — «1» не должен читаться как
    коэффициент из-за «21x» по соседству. Граница числа (не цифра/разделитель
    вплотную к найденному вхождению) обязана быть проверена, иначе валидная
    спека с коротким порогом ложно ловит противоречие форм."""
    quote = "Не допускать превышения левериджа свыше 21x; абсолютный лимит составляет 1 млн долларов."
    assert specs_extract._threshold_form("1", quote) is None
    cov = covenant(clause="6.9", metric="agg(CAPEX, out)", limit="1", quote=quote)
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(quote), set())
    sp = art["clauses"]["6.9"]
    assert "limit_shape_mismatch" not in sp["errors"]
    assert not any(a["kind"] == "limit_shape_mismatch" for a in art["alarms"])


def test_threshold_form_no_collision_with_neighboring_percent(tmp_path, monkeypatch):
    """Ревью раунд 1 (Critical): порог «5» не должен подхватывать форму
    «доля» из чужих «15%» по соседству по той же причине."""
    quote = "Расходы на маркетинг не выше 15% от EBITDA; минимальный порог составляет 5."
    assert specs_extract._threshold_form("5", quote) is None
    cov = covenant(clause="6.10", metric="ratio(agg(CAPEX, out), agg(REVENUE, in))", limit="5", quote=quote)
    monkeypatch.setattr(specs_extract.llm, "call", lambda *a, **k: {"covenants": [cov]})
    art = specs_extract.extract_specs(tmp_path, make_dossier(quote), set())
    sp = art["clauses"]["6.10"]
    assert "limit_shape_mismatch" not in sp["errors"]
    assert not any(a["kind"] == "limit_shape_mismatch" for a in art["alarms"])
