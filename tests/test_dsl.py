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


def test_parse_period_with_quoted_dates_equals_bare():
    # Тот же класс путаницы форм, что у counterparty_in: Gemini на живом
    # прогоне (task-28, третий паттерн) кавычит даты периода —
    # period('2025-01-01', '2025-12-31') вместо голых date-литералов.
    # Содержимое строки в форме даты — та же дата; ловилось как
    # «ожидался литерал ('date',)» и стоило tier=1 у половины сценариев.
    quoted = parse("agg(REVENUE, in, period('2025-01-01', '2025-12-31'))")
    bare = parse("agg(REVENUE, in, period(2025-01-01, 2025-12-31))")
    assert quoted == bare


def test_parse_doc_with_quoted_key_equals_bare():
    # doc('max_related_party_payments') вместо doc(max_related_party_payments)
    # — тоже живой паттерн Gemini (P9 в task-28). Строка в форме
    # идентификатора — тот же ключ.
    assert parse("doc('severance_liability')") == parse("doc(severance_liability)")


def test_parse_quoted_num_literals_equal_bare():
    # Симметрия прощения (ревью PR #11): модель, кавычащая даты и ключи,
    # так же кавычит числа — quarter('1'), min_amount('500000'), const('3.0').
    # Тип обязан совпасть с голой формой (Decimal), иначе signature()-матч
    # с шаблонами разъедется по типу поля.
    assert parse("agg(REVENUE, in, quarter('1'))") == parse("agg(REVENUE, in, quarter(1))")
    assert parse("agg(ALL, out, min_amount('500000'))") == parse("agg(ALL, out, min_amount(500000))")
    assert parse("const('3.0')") == parse("const(3.0)")


def test_parse_quoted_garbage_is_still_error():
    # Прощение только когда содержимое строки соответствует форме ожидаемого
    # литерала: дата не в ISO-форме и ключ с пробелом остаются ошибкой.
    with pytest.raises(DslError):
        parse("agg(REVENUE, in, period('вчера', '2025-12-31'))")
    with pytest.raises(DslError):
        parse("doc('not an identifier')")


def test_parse_agg_kwargs_filters_list_equals_bare_tail():
    # Третья форма той же путаницы (P6 в task-28): модель печатает имя поля
    # AST — agg(..., filters=[period(...), counterparty_in(...)]) — вместо
    # голого хвоста фильтров. Список после filters= — тот же хвост.
    kw = parse("agg(ALL,out,filters=[period(2025-01-01,2025-12-31),counterparty_in(related_parties)])")
    bare = parse("agg(ALL, out, period(2025-01-01, 2025-12-31), counterparty_in(related_parties))")
    assert kw == bare


def test_parse_empty_filters_kwarg_equals_no_tail():
    # Ревью PR #11, раунд 2: самая вероятная форма эха — пустое значение поля,
    # agg(ALL, out, filters=[]). Семантически это agg(ALL, out), неоднозначности
    # нет — в отличие от разрядной запятой, громкое падение здесь не оправдано.
    assert parse("agg(ALL, out, filters=[])") == parse("agg(ALL, out)")


def test_parse_filters_kwarg_outside_agg_tail_is_error():
    with pytest.raises(DslError):
        parse("ratio(filters=[period(2025-01-01,2025-12-31)], agg(REVENUE, in))")
    with pytest.raises(DslError):
        parse("agg(REVENUE, filters=[quarter(1)], in)")


def test_parse_counterparty_in_quoted_single_name_is_singleton_list():
    # Кавычка вокруг обычного имени — не опечатка формы множества, а список
    # из одного контрагента.
    node = parse("agg(ALL, out, counterparty_in('Acme LLP'))")
    assert node.filters[0].setname == ("Acme LLP",)


def test_parse_counterparty_in_literal_описание_множества_равно_имени_множества():
    # Третья форма той же путаницы: модель кладёт в список не контрагентов, а
    # название самого множества человеческими словами. Буквальный матч по
    # токенам не совпадёт ни с одним контрагентом — ячейка гарантированно
    # обнуляется, поэтому описание разрешается в имя множества.
    bare = parse("agg(ALL, out, counterparty_in(related_parties))")
    for text in (
        "agg(ALL, out, counterparty_in(['аффилированные лица']))",
        "agg(ALL, out, counterparty_in(['Связанные стороны']))",
        "agg(ALL, out, counterparty_in(['related parties']))",
        "agg(ALL, out, counterparty_in('аффилированные лица'))",
    ):
        assert parse(text) == bare, text


def test_parse_counterparty_in_literal_описание_необременённых():
    bare = parse("agg(CAPEX, out, counterparty_in(unrestricted_subsidiaries))")
    for text in (
        "agg(CAPEX, out, counterparty_in(['необременённые дочерние компании']))",
        "agg(CAPEX, out, counterparty_in(['unrestricted subsidiaries']))",
    ):
        assert parse(text) == bare, text


def test_parse_counterparty_in_имя_с_юрформой_не_становится_множеством():
    """Основа названия множества встречается и в настоящих именах компаний.

    Промах здесь несимметричен и молчалив: вместо одного контрагента фильтр
    возьмёт весь набор связанных сторон, сумма вырастет на чужие строки, а
    вердикт останется правдоподобным. Обратный промах (не разрешили описание)
    даёт нулевую агрегацию и ловится лестницей фолбэков. Признак юрлица в
    строке — надёжная граница: название множества его не содержит.
    """
    node = parse("agg(ALL, out, counterparty_in(['Affiliated Trading LLP']))")
    assert node.filters[0].setname == ("Affiliated Trading LLP",)
    single = parse("agg(ALL, out, counterparty_in('ТОО Связанные Технологии'))")
    assert single.filters[0].setname == ("ТОО Связанные Технологии",)
    # Описания из живых прогонов юрформы не содержат и разрешаются по-прежнему.
    assert parse("agg(ALL, out, counterparty_in(['аффилированные лица']))") == parse(
        "agg(ALL, out, counterparty_in(related_parties))"
    )


def test_parse_counterparty_in_literal_имена_контрагентов_не_трогаются():
    # Обычный список остаётся буквальным: разрешение описаний не должно
    # проглатывать имена контрагентов.
    node = parse("agg(ALL, out, counterparty_in(['Acme LLP', 'Beta Holding']))")
    assert node.filters[0].setname == ("Acme LLP", "Beta Holding")
    # Смешанный список — тоже буквальный: описание разрешается только тогда,
    # когда весь список является описанием одного и того же множества.
    mixed = parse("agg(ALL, out, counterparty_in(['аффилированные лица', 'Acme LLP']))")
    assert mixed.filters[0].setname == ("аффилированные лица", "Acme LLP")


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


def test_bare_filter_list_in_agg_tail():
    """Голый список фильтров — та же форма, что filters=[...], только без имени поля.

    Живой паттерн приватного прогона: модель печатает хвост agg как
    `[period(...), counterparty_in(...)]`, и грамматика роняла всю спеку
    ковенанта на разборе первого же фильтра. Строгость при этом сохраняется:
    список фильтров легален только в хвосте agg, а список СТРОК внутри
    counterparty_in остаётся списком имён — они различаются содержимым, а не
    местом (фильтр всегда вызов, имя со скобкой).
    """
    bare = parse("agg(ALL, out, [period(2025-01-01, 2025-12-31), counterparty_in(related_parties)])")
    assert bare == parse("agg(ALL, out, period(2025-01-01, 2025-12-31), counterparty_in(related_parties))")
    assert parse("agg(ALL, out, counterparty_in(['Foo LLP']))").filters[0].setname == ("Foo LLP",)
    for bad in (
        "counterparty_in([period(2025-01-01, 2025-12-31)])",
        "ratio(agg(CAPEX, out), [period(2025-01-01, 2025-12-31)])",
        "agg(ALL, out, [period(2025-01-01, 2025-12-31)], extra)",
    ):
        with pytest.raises(DslError):
            parse(bad)
