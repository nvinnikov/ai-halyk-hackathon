"""Маленький и тотальный: всё, что выдала модель, парсится грамматикой до исполнения."""

from decimal import Decimal

import pytest

from dsl import Agg, Cmp, DslError, Ratio, parse, signature, uses_ledger, validate


def test_parse_simple_agg():
    node = parse("agg(REVENUE, in)")
    assert node == Agg(category="REVENUE", sign="in", filters=())


def test_parse_nested_with_filters():
    node = parse("ratio(agg(ALL, out, counterparty_in(related_parties)), agg(REVENUE, in, quarter(4)))")
    assert isinstance(node, Ratio)
    assert node.num.filters[0].setname == "related_parties"


def test_parse_literal_set_and_desc_filter():
    node = parse("agg(CAPEX, out, counterparty_in(['A Co', 'B Co']), desc_contains('subsidiary'))")
    assert node.filters[0].setname == ("A Co", "B Co")
    assert node.filters[1].s == "subsidiary"


def test_parse_counterparty_in_quoted_set_name_equals_bare_keyword():
    # Модель иногда путает две формы аргумента counterparty_in и кавычит имя
    # известного множества вместо голого идентификатора: counterparty_in
    # ('related_parties') вместо counterparty_in(related_parties). Строка,
    # совпадающая с именем множества, — то же множество, а не буквальный
    # контрагент с таким именем.
    quoted = parse("agg(ALL, out, counterparty_in('related_parties'))")
    bare = parse("agg(ALL, out, counterparty_in(related_parties))")
    assert quoted == bare


def test_parse_counterparty_in_quoted_single_name_is_singleton_list():
    # Кавычка вокруг обычного имени — не опечатка формы множества, а список
    # из одного контрагента.
    node = parse("agg(ALL, out, counterparty_in('Acme LLP'))")
    assert node.filters[0].setname == ("Acme LLP",)


def test_parse_doc_const_cmp():
    assert parse("doc(severance_liability)").key == "severance_liability"
    assert parse("const(4000000)").value == Decimal("4000000")
    trig = parse("gt(agg(FINANCING, in), const(4000000))")
    assert isinstance(trig, Cmp) and trig.op == "gt"


def test_parse_tolerates_surrounding_whitespace():
    # LLM почти всегда отдаёт строку с хвостовым переводом строки — это не мусор.
    assert parse("agg(REVENUE, in)\n") == Agg(category="REVENUE", sign="in", filters=())
    assert parse("  agg(REVENUE, in)  ") == Agg(category="REVENUE", sign="in", filters=())


@pytest.mark.parametrize(
    "bad",
    [
        "__import__('os')",
        "agg(REVENUE)",  # не хватает sign
        "agg(REVENUE, sideways)",  # неизвестный sign
        "eval(1)",  # неизвестная функция
        "agg(REVENUE, in) + 1",  # операторов в грамматике нет
        "period(2025-01-01, 2025-12-31)",  # фильтр вне agg
        "",
    ],
)
def test_rejects_anything_outside_grammar(bad):
    with pytest.raises(DslError):
        parse(bad)


def test_validate_category_and_fact_keys():
    assert validate(parse("agg(NOPE, out)"), set()) != []
    assert validate(parse("doc(missing)"), {"present"}) != []
    assert validate(parse("doc(present)"), {"present"}) == []
    assert validate(parse("agg(OPEX_TOTAL, net)"), set()) == []


def test_signature_ignores_constants():
    a = signature(parse("ratio(agg(CAPEX, out), const(2))"))
    b = signature(parse("ratio(agg(CAPEX, out), const(9))"))
    c = signature(parse("ratio(agg(TAX, out), const(2))"))
    assert a == b != c


def test_signature_ignores_sign():
    # Извлечённая спека с net обязана матчиться с out-шаблоном (решение из шапки плана).
    assert signature(parse("agg(CAPEX, out)")) == signature(parse("agg(CAPEX, net)"))
    assert signature(parse("agg(CAPEX, out)")) != signature(parse("agg(TAX, out)"))


def test_uses_ledger():
    assert uses_ledger(parse("agg(REVENUE, in)"))
    assert not uses_ledger(parse("ratio(doc(a), doc(b))"))
