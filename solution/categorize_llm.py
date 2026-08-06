"""Второй ярус категоризации через LLM для непокрытых правилами описаний.

Уникальные описания батчатся по 50, вызывается LLM с детерминированным
порядком (отсортированные), результат кэшируется на уровне llm.call.
Ответ валидируется против LEAVES; вне таксономии → OTHER.
"""

import hashlib

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


def _ordered(descriptions: list[str], order: str) -> list[str]:
    """Порядок описаний в пачке. Рабочий путь всегда sorted; остальные два
    нужны замеру разброса (спека 5.2.1): другой порядок даёт другой промпт,
    другой ключ кэша и, значит, независимый ответ модели при той же задаче."""
    unique = sorted(set(descriptions))
    if order == "sorted":
        return unique
    if order == "reverse":
        return list(reversed(unique))
    if order == "hash":
        return sorted(unique, key=lambda d: hashlib.sha256(d.encode()).hexdigest())
    raise ValueError(f"unknown order {order!r}")


def categorize_batch(descriptions: list[str], order: str = "sorted") -> tuple[dict[str, str], list[dict]]:
    """Категоризирует список описаний через LLM пачками по 50.

    Параметры:
        descriptions: уникальные описания банковских операций
        order: порядок описаний в пачке — "sorted" (рабочий путь), "reverse"
            или "hash" (замер разброса, спека 5.2.1)

    Возвращает:
        (маппинг {description → category}, список алярмов)
        Если LLM предложит категорию вне LEAVES → остаётся OTHER + алярм category_rejected.
    """
    # order валидируется до проверки на пустой ввод: иначе опечатка в имени
    # перестановки (замер разброса, 5.2.1) на пустом списке пройдёт молча,
    # а не пустой список — тихого провала здесь допускать нельзя.
    unique = _ordered(descriptions, order)
    if not descriptions:
        return {}, []

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

        # Вызов LLM с кэшем. Fail-open: любой сбой (SchemaRejected, CassetteMiss,
        # BudgetExhausted, сеть) стоит одного батча OTHER с алярмом, а не всего
        # прогона до расчёта — второй ярус лишь уточняет первый.
        try:
            resp = llm.call(prompt, CAT_SCHEMA, SCHEMA_VERSION)
        except Exception as exc:
            alarms.append({"kind": "categorize_failed", "batch_start": batch[0], "error": repr(exc)})
            continue

        # Парсим ответ и валидируем категорию против таксономии
        returned: dict[str, str] = {}
        for item in resp.get("categories", []):
            desc = item.get("description", "")
            cat = item.get("category", "")
            if cat not in LEAVES:
                alarms.append(
                    {
                        "kind": "category_rejected",
                        "description": desc,
                        "returned": cat,
                    }
                )
                cat = "OTHER"
            returned[desc] = cat

        # Round-trip: ответ сверяется с батчем. Пропущенное или переписанное
        # моделью описание — алярм, а не молчаливый OTHER: тихий провал здесь —
        # ровно то, ради чего ярус вводился.
        for desc in batch:
            if desc in returned:
                result[desc] = returned.pop(desc)
            else:
                alarms.append({"kind": "category_missing", "description": desc})
        for desc in sorted(returned):
            alarms.append({"kind": "category_unmatched_description", "returned": desc})

    return result, alarms
