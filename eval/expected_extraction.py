"""Размеченный вручную эталон извлечения (бывшие facts.py и SPECS).

12 заёмщиков × (связанные стороны, реклассификации, отсечения, добавки,
курсы) плюс 36 пар (метрика, порог). Вопрос к LLM-слою измерим:
восстанавливает ли он этот файл из PDF (раздел 7 спеки, eval №1)?
"""

from decimal import Decimal
from typing import Any

# Any по значению: набор ключей у каждого сценария свой (reclass, exclude,
# fx_rates, amount_override, ...), и потребители достают их через .get с дефолтом.
FACTS: dict[str, dict[str, Any]] = {
    # scenario_id -> факты досье
    "B1": {  # ACC-7201 Ekibastuz Energy JSC
        "related_parties": ["Ertis Capital LLP"],
        "reclass": [{"txn": "TXN-B1-0020", "to": "INTEREST"}],
    },
    "B4": {  # ACC-7204 Shymkent Refinery JSC
        # Shymkent Fuel Distributors — 48.0% голосов при пороге 30.0%; в
        # related_abs не попадает: все её строки у B4 — поступления.
        "related_parties": ["Kazyna Capital LLP", "Shymkent Fuel Distributors LLP"],
        "exclude": ["TXN-B4-0026"],  # отсечение: переход рисков в январе 2026
    },
    "P1": {  # ACC-7801 Aktau Port Services JSC
        "related_parties": ["Aktau Holdings LLP"],
        "exclude": ["TXN-P1-0045"],  # услуги оказаны в 2026
    },
    "P2": {  # ACC-7802 Almaty Cold Chain JSC   (таблица KYC — vision)
        "related_parties": ["Zhetysu Capital Partners LLP"],
        "reclass": [{"txn": None, "counterparty": "Tien Shan Advisory Bureau", "to": "OTHER_OPEX"}],
        "vision": ["63e162bd710b.pdf#p2"],
    },
    "P3": {  # ACC-7803 Shymkent Refinery Services JSC
        "related_parties": ["Turan Capital LLP"],
        # Курс выведен из пары зеркальных платежей, интервал в документе не
        # указан — пустые границы, стадия fx помечает это в трейсе.
        "fx_rates": [
            {
                "currency": "EUR",
                "usd_per_unit": str((Decimal("83690.23") / Decimal("72146.75")).quantize(Decimal("1E-9"))),
                "effective_from": "",
                "effective_to": "",
                "source_quote": "выведен из пары зеркальных платежей казначейства",
                "derivation": "paired_payment",
                "doc_date": "",
                "doc_hash": "",
            }
        ],
    },
    "P4": {  # ACC-7804 Aktobe Grain Terminal JSC  (таблица добавок — vision)
        "related_parties": ["Aral Capital Partners LLP"],
        "ebitda_addbacks": ["251338.94", "342905.28", "481247.63"],
        "addback_materiality": "300000.00",
        "vision": ["2ed0b2ee4b57.pdf#p3,p4"],
    },
    "P5": {  # ACC-7805 Ekibastuz Power Services JSC
        "related_parties": ["Sarybel Capital LLP"],
    },
    "P6": {  # ACC-7806 Taraz Cement Works JSC  (KYC целиком скан — vision)
        "related_parties": ["Taraz Holding Group LLP"],
        "vision": ["f3fa6d20c8a1.pdf#all"],
    },
    "P7": {  # ACC-7807 Atyrau Pipeline Services JSC
        "related_parties": ["Atyrau Holding Group LLP"],
        "amount_override": {"TXN-P7-0033": "-486204.19"},  # записка казначейства
    },
    "P8": {  # ACC-7808 Kyzylorda Drilling Services JSC
        "related_parties": ["Syrdarya Capital Holding LLP"],
        "amount_override": {"TXN-P8-0031": "-884204.16"},
        "severance_liability": "918447.52",
    },
    "P9": {  # ACC-7809 Zhezkazgan Mining Services JSC  (таблица залога — vision)
        "related_parties": ["Ulytau Capital LLP"],
        "unrestricted_subsidiaries": ["Zhezkazgan Processing Holdings LLP"],
        "vision": ["aaf665cbc612.pdf#p2"],
    },
    "P10": {  # ACC-7810 Karaganda Logistics Terminal JSC
        "related_parties": ["Saryarka Capital Partners LLP"],
        "reclass": [{"txn": None, "counterparty": "Tengiz Risk Engineering Bureau", "to": "INSURANCE"}],
        # TXN-P10-0012 и TXN-P10-0021 рассмотрены аудитором и оставлены как есть
    },
}

# --- спецификации ячеек -----------------------------------------------------
# (метрика, направление, порог)  direction: "max" — не превышать, "min" — не ниже
SPECS = {
    "B1": {
        "6.1": ("icr", "min", 2.00),
        "6.2": ("max_overhead_line", "max", 1_500_000),
        "6.3": ("related_abs", "max", 500_000),
    },
    "B4": {
        "6.1": ("revenue_q4", "min", 3_500_000),
        "6.2": ("capex", "max", 2_000_000),
        "6.3": ("related_abs", "max", 500_000),
    },
    "P1": {
        "6.1": ("capital_intensity", "max", 0.42),
        "6.2": ("revenue", "min", 7_100_000),
        "6.3": ("related_abs", "max", 450_000),
    },
    "P2": {
        "6.1": ("sources_cover", "min", 1.20),
        "6.2": ("capex", "max", 3_000_000),
        "6.3": ("related_share_revenue", "max", 0.03),
    },
    "P3": {
        "6.1": ("springing_leverage", "max", 1.70, {"trigger_financing": 4_000_000}),
        "6.2": ("revenue", "min", 6_500_000),
        "6.3": ("related_abs", "max", 400_000),
    },
    "P4": {
        "6.1": ("adj_ebitda_margin", "min", 0.28),
        "6.2": ("capex", "max", 1_800_000),
        "6.3": ("related_share_revenue", "max", 0.04),
    },
    "P5": {
        "6.1": ("group_capex_to_ebitda", "max", 9.00),
        "6.2": ("revenue", "min", 7_500_000),
        "6.3": ("related_abs", "max", 260_000),
    },
    "P6": {
        "6.1": ("related_share_opex", "max", 0.08),
        "6.2": ("revenue_cover_payroll_utilities", "min", 3.00),
        "6.3": ("capex", "max", 1_600_000),
    },
    "P7": {
        "6.1": ("tax_utility_to_ebitda", "max", 0.30),
        "6.2": ("revenue", "min", 8_700_000),
        "6.3": ("related_abs", "max", 275_000),
    },
    "P8": {
        "6.1": ("staff_liabilities", "max", 4_000_000),
        "6.2": ("capex", "max", 2_100_000),
        "6.3": ("related_share_revenue", "max", 0.04),
    },
    "P9": {
        "6.1": ("unrestricted_transfer_share", "max", 0.15),
        "6.2": ("revenue", "min", 6_900_000),
        "6.3": ("related_abs", "max", 225_000),
    },
    "P10": {
        "6.1": ("insurance_cover", "min", 0.20),
        "6.2": ("revenue_less_max_overhead", "min", 5_000_000),
        "6.3": ("related_share_revenue", "max", 0.05),
    },
}
