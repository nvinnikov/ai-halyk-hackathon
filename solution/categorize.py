"""Категоризация транзакций по назначению платежа.

Контрагент систематически не соответствует сути операции (Foxridge Stationery
платит налог на прибыль), поэтому классифицируем строго по description.
"""

import re

# Порядок важен: первое совпадение выигрывает.
RULES = [
    ("CAPEX", r"purchase of .*equipment|transfer of .*equipment to subsidiary"),
    ("REVENUE", r"sales settlement"),
    ("FINANCING", r"facility drawdown"),
    ("INTEREST", r"\binterest\b|interest coupon"),
    ("PAYROLL", r"payroll"),
    ("INSURANCE", r"insurance|fidelity bond"),
    (
        "MARKETING",
        r"marketing|ad campaign|media buy|advertis|sponsorship|exhibition stand|"
        r"printed .*collateral|point-of-sale|outdoor .*site hire|trade press|"
        r"radio ad|product launch|digital media|customer newsletter|"
        r"vehicle livery|photography and artwork|research panel",
    ),
    ("TAX", r"\btax\b|\bvat\b|customs duty|municipal .*levy|excise|withholding"),
    ("UTILITIES", r"electricity|water|natural gas|district heating|compressed air|utility|utilities"),
    ("TELECOM", r"telecom"),
    ("RENT", r"\brent\b|\blease\b|rental"),
    # Консультационные услуги — отдельная статья: аудиторы переклассифицируют
    # именно их, и в EBITDA как «Операционные расходы» они не входят.
    ("CONSULTING", r"advisory engagement|management .*retainer|retainer fee"),
    (
        "OPEX",
        r"operating and maintenance|servicing and operating|servicing contract|"
        r"servicing|remediation|cleaning and clearance|arbitration and legal",
    ),
]


def categorize(description: str) -> str:
    d = description.lower()
    for name, pattern in RULES:
        if re.search(pattern, d):
            return name
    return "OTHER"
