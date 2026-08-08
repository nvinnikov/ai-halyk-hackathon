"""Библиотека шаблонов: вылизанные реализации 19 известных метрик в DSL.

Роль после решения «out/net»: шаблоны остаются на out и держат парити с
легаси; match_signature матчит по sign-нормализованной сигнатуре (знак в
signature затёрт), поэтому извлечённая спека с net совпадает с out-шаблоном.
В extracted-режиме solve исполняет канонический DSL шаблона (TEMPLATES),
а не сырую спеку — имя шаблона (от match_heading, резервно match_signature)
даёт заведомо вылизанную реализацию вместо возможно неточной формулы модели.

ebitda_total_opex — второе легитимное прочтение EBITDA (вся операционка,
а не только OTHER_OPEX): производных от него не строим, запись нужна как
узнаваемая сигнатура — какое прочтение выбрал договор, видно по имени.

match_heading — ОСНОВНОЙ путь матча спеки с шаблоном: заголовок пункта
однозначно определяет метрику (19 уникальных заголовков на 36 ячеек при 19
метриках, распределение один-в-один), тогда как тела пунктов почти все
разные и матч по сигнатуре DSL — ненадёжный резерв. TEMPLATE_HEADINGS
построен из реальных заголовков пунктов публичных договоров (пять из
девятнадцати — на английском при русском теле, ключ языконезависим: регистр,
пунктуация и цифры в заголовке роли не играют)."""

import re

from dsl import parse, signature

_NON_WORD_OR_DIGIT = re.compile(r"[\d\W]+", re.UNICODE)


def title_key(text: str) -> str:
    """Языконезависимый нормализованный ключ заголовка пункта: нижний
    регистр, без цифр и пунктуации, схлопнутые пробелы. Единственная
    реализация — specs_extract берёт её отсюда для title_key спеки, здесь же
    она строит TEMPLATE_HEADINGS."""
    norm = " ".join(text.lower().split())
    stripped = _NON_WORD_OR_DIGIT.sub(" ", norm)
    return " ".join(stripped.split())


# Дословные заголовки пунктов из публичных договоров (без номера пункта и без
# имени заёмщика — грамматика TEMPLATE_HEADINGS в плане прямо это разрешает).
_TEMPLATE_HEADING_TEXT: dict[str, str] = {
    "icr": "Минимальный коэффициент покрытия процентов",
    "max_overhead_line": "Individual Overhead Line Ceiling",
    "related_abs": "Максимальные платежи связанным сторонам",
    "related_share_revenue": "Maximum Related-Party Payments as a Proportion of Revenue",
    "related_share_opex": "Максимальная доля платежей связанным сторонам в операционных расходах",
    "revenue": "Минимальная выручка по категории",
    "revenue_q4": "Минимальная выручка за четвёртый квартал",
    "capex": "Максимальные расходы по категории",
    "capital_intensity": "Maximum Capital Intensity Ratio",
    "sources_cover": "Minimum Cover of Applications by Sources",
    "springing_leverage": "Springing Drawdown Leverage Test",
    "adj_ebitda_margin": "Минимальная скорректированная рентабельность по EBITDA",
    "group_capex_to_ebitda": "Максимальное отношение капитальных затрат Группы к EBITDA Заёмщика",
    "tax_utility_to_ebitda": "Максимальное отношение налоговой и коммунальной нагрузки к EBITDA",
    "staff_liabilities": "Максимальные совокупные обязательства по персоналу",
    "revenue_cover_payroll_utilities": (
        "Минимальное покрытие расходов на персонал и коммунальные услуги выручкой"
    ),
    "unrestricted_transfer_share": (
        "Максимальная доля активов, переданных неограниченным дочерним организациям"
    ),
    "insurance_cover": "Минимальное страховое покрытие расходов на содержание помещений",
    "revenue_less_max_overhead": "Минимальная выручка за вычетом наибольшей статьи накладных расходов",
    # ebitda_total_opex не имеет заголовка в датасете: второе прочтение
    # EBITDA существует только как сигнатура, живым пунктом не встречается.
}

TEMPLATE_HEADINGS: dict[str, str] = {title_key(h): name for name, h in _TEMPLATE_HEADING_TEXT.items()}


# Значимые слова заголовка: короткие — предлоги и союзы, они одинаковы у всех
# заголовков и только раздували бы сходство.
_MIN_HEADING_TOKEN = 3
_HEADING_TOKENS: dict[str, frozenset[str]] = {
    name: frozenset(w for w in title_key(h).split() if len(w) >= _MIN_HEADING_TOKEN)
    for name, h in _TEMPLATE_HEADING_TEXT.items()
}

# Сходство меряется в целых процентах пересечения к объединению токенов.
# Порог и требуемый отрыв закреплены тестом
# (test_heading_similarity_separates_own_from_foreign), который меряет ровно
# то, что сравнивает код: на пертурбированном заголовке — сходство с
# победителем и отрыв от следующего за ним.
#
# Запас неодинаков: по сходству он комфортный, по отрыву — ОДИН процентный
# пункт. Выброшенное слово уменьшает объединение и поднимает сходство с чужими
# шаблонами тоже, поэтому родственные заголовки подходят близко. Любое новое
# родственное имя в библиотеке этот запас съест — и тогда упадёт тест, а не
# приватный прогон.
_MIN_HEADING_SIMILARITY_PCT = 60
_MIN_HEADING_MARGIN_PCT = 15


def heading_similarity_pct(a: frozenset[str], b: frozenset[str]) -> int:
    """Пересечение к объединению в целых процентах; пустые наборы — ноль."""
    union = a | b
    return 100 * len(a & b) // len(union) if union else 0


def match_heading(heading_key: str) -> str | None:
    """Шаблон по заголовку пункта: точное совпадение, иначе близкое по словам.

    Точный поиск по словарю обрывался на любой переформулировке: одно слово
    иначе — и ни один из 19 заголовков не срабатывал. Замер LOBO показывает
    цену обрыва: 34.50 против 29.50, то есть библиотека несёт 5.00 балла, а
    формулировки приватных договоров нам неизвестны.

    Неверный шаблон, однако, хуже отсутствия матча: формула подменится молча и
    ячейка посчитается не тем. Поэтому близкий матч принимается, только когда
    победитель один и отрыв от второго решительный; при сомнении — None и
    сырой DSL спеки, как раньше. Нестрогое срабатывание не молчит: solve пишет
    алярм heading_matched_loosely с ключом и выбранным шаблоном.

    Известная граница. Если заголовок теряет ровно те слова, которыми он
    отличается от более общего собрата, разделить их нечем: у законного матча в
    худшем случае ровно такой же отрыв, как у вырожденного (заголовок про
    квартальную выручку без двух последних слов — это буквально заголовок про
    выручку). Порог здесь не помогает, помогает алярм: в трейсе видно, на какой
    шаблон увело.
    """
    exact = TEMPLATE_HEADINGS.get(heading_key)
    if exact is not None:
        return exact
    want = frozenset(w for w in heading_key.split() if len(w) >= _MIN_HEADING_TOKEN)
    if not want:
        return None
    # Сортировка по (сходство, имя): имена уникальны, порядок полный и
    # воспроизводимый — при равном сходстве победителя всё равно не будет.
    ranked = sorted(
        ((heading_similarity_pct(want, toks), name) for name, toks in _HEADING_TOKENS.items()),
        reverse=True,
    )
    best, name = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0
    if best < _MIN_HEADING_SIMILARITY_PCT or best - runner_up < _MIN_HEADING_MARGIN_PCT:
        return None
    return name


_EBITDA = "sub(agg(REVENUE, in), agg(OTHER_OPEX, out))"
_RELATED = "agg(ALL, out, counterparty_in(related_parties))"

TEMPLATES: dict[str, str] = {
    "icr": f"ratio({_EBITDA}, agg(INTEREST, out))",
    "max_overhead_line": "max(agg(PAYROLL, out), agg(UTILITIES, out))",
    "related_abs": _RELATED,
    "related_share_revenue": f"ratio({_RELATED}, agg(REVENUE, in))",
    "related_share_opex": f"ratio({_RELATED}, agg(OTHER_OPEX, out))",
    "revenue": "agg(REVENUE, in)",
    "revenue_q4": "agg(REVENUE, in, quarter(4))",
    "capex": "agg(CAPEX, out)",
    "capital_intensity": "ratio(agg(CAPEX, out), add(agg(OTHER_OPEX, out), agg(RENT, out)))",
    "sources_cover": (
        "ratio(add(agg(REVENUE, in), agg(FINANCING, in)), add(agg(OTHER_OPEX, out), agg(CAPEX, out)))"
    ),
    "springing_leverage": f"ratio(agg(FINANCING, in), {_EBITDA})",
    "adj_ebitda_margin": (f"ratio(add({_EBITDA}, doc(ebitda_addbacks_material_total)), agg(REVENUE, in))"),
    # Числителя нет в леджере: капитальные затраты ГРУППЫ определяются по
    # консолидированной отчётности материнской компании, а леджер знает только
    # затраты самого заёмщика — это другая величина, а не приближение к ней.
    # Ключ считает код по отчётности группового уровня (facts_extract.
    # _group_capex); нет её в досье — спека невалидна и ячейка уходит на
    # лестницу, что честнее уверенно посчитанной не той суммы.
    "group_capex_to_ebitda": f"ratio(doc(group_capex), {_EBITDA})",
    "tax_utility_to_ebitda": f"ratio(add(agg(TAX, out), agg(UTILITIES, out)), {_EBITDA})",
    "staff_liabilities": "add(agg(PAYROLL, out), doc(severance_liability))",
    "revenue_cover_payroll_utilities": (
        "ratio(agg(REVENUE, in), add(agg(PAYROLL, out), agg(UTILITIES, out)))"
    ),
    "unrestricted_transfer_share": (
        "ratio(agg(CAPEX, out, counterparty_in(unrestricted_subsidiaries), "
        "desc_contains('subsidiary')), agg(CAPEX, out))"
    ),
    "insurance_cover": "ratio(agg(INSURANCE, out), add(agg(RENT, out), agg(UTILITIES, out)))",
    "revenue_less_max_overhead": "sub(agg(REVENUE, in), max(agg(PAYROLL, out), agg(TAX, out)))",
    "ebitda_total_opex": "sub(agg(REVENUE, in), agg(OPEX_TOTAL, out))",
}

_BY_SIGNATURE = {signature(parse(text)): name for name, text in TEMPLATES.items()}


def match_signature(node) -> str | None:
    return _BY_SIGNATURE.get(signature(node))
