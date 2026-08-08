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


def match_heading(heading_key: str) -> str | None:
    return TEMPLATE_HEADINGS.get(heading_key)


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
    "group_capex_to_ebitda": f"ratio(agg(CAPEX, out), {_EBITDA})",
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
