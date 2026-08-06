"""Факты, извлечённые из документов заёмщика.

В боевом пайплайне это результат работы LLM над досье (реклассификации, KYC,
пороги, отсечения периода, добавки к EBITDA). Здесь зафиксировано то, что
извлечено из публичного датасета — включая страницы, которые не отдают
текстовый слой и читаются только vision-моделью (помечены `vision`).
"""

from typing import Any

# Any по значению: набор ключей у каждого сценария свой (reclass, exclude, fx,
# amount_override, ...), и потребители достают их через .get с дефолтом.
FACTS: dict[str, dict[str, Any]] = {
    # scenario_id -> факты досье
    "B1": {  # ACC-7201 Ekibastuz Energy JSC
        "related_parties": ["Ertis Capital LLP"],
        "reclass": [{"txn": "TXN-B1-0020", "to": "INTEREST"}],
    },
    "B4": {  # ACC-7204 Shymkent Refinery JSC
        "related_parties": ["Kazyna Capital LLP"],
        "exclude": ["TXN-B4-0026"],  # отсечение: переход рисков в январе 2026
    },
    "P1": {  # ACC-7801 Aktau Port Services JSC
        "related_parties": ["Aktau Holdings LLP"],
        "exclude": ["TXN-P1-0045"],  # услуги оказаны в 2026
    },
    "P2": {  # ACC-7802 Almaty Cold Chain JSC   (таблица KYC — vision)
        "related_parties": ["Zhetysu Capital Partners LLP"],
        "reclass": [{"txn": None, "counterparty": "Tien Shan Advisory Bureau", "to": "OPEX"}],
        "vision": ["63e162bd710b.pdf#p2"],
    },
    "P3": {  # ACC-7803 Shymkent Refinery Services JSC
        "related_parties": ["Turan Capital LLP"],
        "fx": {"EUR": 83690.23 / 72146.75},
    },
    "P4": {  # ACC-7804 Aktobe Grain Terminal JSC  (таблица добавок — vision)
        "related_parties": ["Aral Capital Partners LLP"],
        "ebitda_addbacks": [251338.94, 342905.28, 481247.63],
        "addback_materiality": 300000.00,
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
        "amount_override": {"TXN-P7-0033": -486204.19},  # записка казначейства
    },
    "P8": {  # ACC-7808 Kyzylorda Drilling Services JSC
        "related_parties": ["Syrdarya Capital Holding LLP"],
        "amount_override": {"TXN-P8-0031": -884204.16},
        "severance_liability": 918447.52,
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
