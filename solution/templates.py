"""Библиотека шаблонов: вылизанные реализации 19 известных метрик в DSL.

Роль после решения «out/net»: шаблоны остаются на out и держат парити с
легаси; match_signature матчит по sign-нормализованной сигнатуре (знак в
signature затёрт), поэтому извлечённая спека с net совпадает с out-шаблоном.
В extracted-режиме при совпадении сигнатуры исполняется DSL извлечённой
спеки, а имя шаблона используется для family-приора, трейса и LOBO.

ebitda_total_opex — второе легитимное прочтение EBITDA (вся операционка,
а не только OTHER_OPEX): производных от него не строим, запись нужна как
узнаваемая сигнатура — какое прочтение выбрал договор, видно по имени.
"""

from dsl import parse, signature

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
