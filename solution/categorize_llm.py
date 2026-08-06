"""Второй ярус категоризации через LLM для непокрытых правилами описаний.

Уникальные описания батчатся по 50, вызывается LLM с детерминированным
порядком (отсортированные), результат кэшируется на уровне llm.call.
Ответ валидируется против LEAVES; вне таксономии → OTHER.
"""

import llm
from taxonomy import LEAVES

CAT_SCHEMA = {
    "type": "object",
    "properties": {
        "categories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["description", "category"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["categories"],
    "additionalProperties": False,
}

CAT_PROMPT = """Разнеси описания банковских транзакций по категориям. Категории
(выбирай ровно одну из списка, OTHER — только если ничего не подходит):
{taxonomy}

REVENUE — поступления от продаж; PAYROLL — оплата труда; UTILITIES — коммунальные;
RENT — аренда; TAX — налоги и сборы; INTEREST — проценты по займам; CAPEX — покупка
оборудования и капвложения; INSURANCE — страхование; FINANCING — кредитные транши;
MARKETING — реклама и продвижение; TELECOM — связь; CONSULTING — консультационные
услуги; OTHER_OPEX — прочие операционные расходы (обслуживание, ремонт, юр. услуги).

Описания:
{descriptions}"""

BATCH_SIZE = 50
SCHEMA_VERSION = "1"


def categorize_batch(descriptions: list[str]) -> tuple[dict[str, str], list[dict]]:
    """Категоризирует список описаний через LLM пачками по 50.

    Параметры:
        descriptions: уникальные описания банковских операций

    Возвращает:
        (маппинг {description → category}, список алярмов)
        Если LLM предложит категорию вне LEAVES → остаётся OTHER + алярм category_rejected.
    """
    if not descriptions:
        return {}, []

    # Дедупликация и сортировка для детерминизма
    unique = sorted(set(descriptions))

    result = {}
    alarms = []
    taxonomy_str = ", ".join(sorted(LEAVES - {"OTHER"}))

    # Батчим по 50
    for i in range(0, len(unique), BATCH_SIZE):
        batch = unique[i : i + BATCH_SIZE]

        # Форматируем описания для промпта
        descriptions_str = "\n".join(f"{j + 1}. {desc}" for j, desc in enumerate(batch))

        prompt = CAT_PROMPT.format(
            taxonomy=taxonomy_str,
            descriptions=descriptions_str,
        )

        # Вызов LLM с кэшем
        resp = llm.call(prompt, CAT_SCHEMA, SCHEMA_VERSION)

        # Парсим ответ и валидируем
        for item in resp.get("categories", []):
            desc = item.get("description", "")
            cat = item.get("category", "")

            # Валидация против таксономии
            if cat not in LEAVES:
                alarms.append(
                    {
                        "kind": "category_rejected",
                        "description": desc,
                        "returned": cat,
                    }
                )
                cat = "OTHER"

            result[desc] = cat

    return result, alarms
