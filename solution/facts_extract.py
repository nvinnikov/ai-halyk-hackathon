"""Факты досье (5.2/5.3): LLM извлекает с цитатами, код сливает детерминированно."""

import hashlib
from decimal import Decimal
from pathlib import Path

import llm
from guard import DATA_NOT_COMMANDS, sanitize_document, verify_quote
from stages import artifact
from taxonomy import LEAVES

FACTS_VERSION = 2  # v2: paired_payment fx_rate теряет границы интервала (см. _merge_doc)
SCHEMA_VERSION = "facts-1"
RESOLVE_SCHEMA_VERSION = "docfact-1"

FACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "related_parties": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["name", "quote"],
                "additionalProperties": False,
            },
        },
        "unrestricted_subsidiaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["name", "quote"],
                "additionalProperties": False,
            },
        },
        "reclassifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "txn_id": {"type": ["string", "null"]},
                    "counterparty": {"type": ["string", "null"]},
                    "to_category": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["txn_id", "counterparty", "to_category", "quote"],
                "additionalProperties": False,
            },
        },
        "excluded_txns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"txn_id": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["txn_id", "quote"],
                "additionalProperties": False,
            },
        },
        "amount_corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "txn_id": {"type": "string"},
                    "corrected_amount": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["txn_id", "corrected_amount", "quote"],
                "additionalProperties": False,
            },
        },
        "fx_rates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "currency": {"type": "string"},
                    "usd_per_unit": {"type": "string"},
                    "effective_from": {"type": "string"},
                    "effective_to": {"type": "string"},
                    "source_quote": {"type": "string"},
                    "derivation": {"type": "string", "enum": ["table", "paired_payment"]},
                },
                "required": [
                    "currency",
                    "usd_per_unit",
                    "effective_from",
                    "effective_to",
                    "source_quote",
                    "derivation",
                ],
                "additionalProperties": False,
            },
        },
        "numeric_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["key", "value", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "related_parties",
        "unrestricted_subsidiaries",
        "reclassifications",
        "excluded_txns",
        "amount_corrections",
        "fx_rates",
        "numeric_facts",
    ],
    "additionalProperties": False,
}

FACTS_PROMPT = """Ты извлекаешь факты из финансового документа заёмщика для проверки
ковенантов. Извлекай ТОЛЬКО то, что написано в документе, с дословной цитатой
(quote) для каждого факта. Не выводи ничего из общих знаний.

{focus}

Правила:
- related_parties: контрагенты, признанные связанными сторонами (KYC, договор).
- unrestricted_subsidiaries: дочерние компании, признанные необременёнными.
- reclassifications: аудитор/отчёт перенёс операцию в другую категорию; указывай
  txn_id, если он назван, иначе counterparty; to_category — из списка: {taxonomy}.
- excluded_txns: операции, исключённые из расчёта (отсечение периода, переход рисков).
- amount_corrections: операции с исправленной суммой (записки казначейства);
  corrected_amount — строка с точным числом, расход со знаком минус.
- fx_rates: обменные курсы; usd_per_unit — сколько долларов за единицу валюты,
  строкой; derivation: table — из таблицы курсов, paired_payment — выведен из
  пары зеркальных платежей.
- numeric_facts: прочие числовые обязательства и показатели, названные в
  документе и относящиеся к ковенантам (например обязательство по выходным
  пособиям — ключ severance_liability; добавки к EBITDA — ключи
  ebitda_addback_1..N и ebitda_addback_materiality; консолидированный CapEx
  группы — ключ group_capex). Ключ — snake_case по-английски, value — строка
  с числом без разделителей.

Пустые списки допустимы. Верни результат через emit.

<document type="{doc_type}">
{text}
</document>"""

FOCUS = {
    "kyc": "Фокус: связанные стороны, необременённые дочки, пороги связанности.",
    "audit_report": "Фокус: реклассификации операций и исключения из расчёта.",
    "financial_notes": "Фокус: добавки к EBITDA, обязательства, курсы валют.",
    "treasury_memo": "Фокус: исправления сумм конкретных транзакций, курсы валют.",
    "agreement": "Фокус: обязательства и числовые показатели, названные в договоре.",
    "other": "Фокус: любые факты из перечисленных ниже.",
}

RESOLVE_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "value": {"type": "string"},
        "quote": {"type": "string"},
    },
    "required": ["found", "value", "quote"],
    "additionalProperties": False,
}

RESOLVE_PROMPT = """В документах заёмщика нужно найти число: {description} (ключ {key}).
Если число прямо названо в тексте — верни found=true, value строкой без
разделителей (расход со знаком минус) и дословную цитату quote.
Если его в тексте нет — found=false, value и quote пустые. Не вычисляй и не
оценивай — только дословное число из текста.

{documents}"""


def _empty_facts() -> dict:
    return {
        "related_parties": [],
        "related_quotes": {},
        "unrestricted_subsidiaries": [],
        "subsidiary_quotes": {},
        "reclass": [],
        "exclude": [],
        "exclude_quotes": {},
        "amount_override": {},
        "override_quotes": {},
        "fx_rates": [],
        "doc_facts": {},
        "doc_fact_quotes": {},
        "ebitda_addbacks": [],
        "addback_materiality": "0",
        "alarms": [],
    }


def _number_ok(value: str) -> bool:
    try:
        Decimal(value)
        return True
    except Exception:
        return False


def _merge_doc(facts: dict, raw: dict, doc: dict, text: str) -> None:
    def verified(quote: str, kind: str) -> bool:
        """Факт без цитаты из текста не принимается: это либо инъекция, либо
        галлюцинация — контракт guard (задача 3a)."""
        if verify_quote(quote, text):
            return True
        facts["alarms"].append({"kind": "quote_unverified", "field": kind, "file": doc["file"]})
        return False

    def number_ok(value: str, kind: str) -> bool:
        if _number_ok(value):
            return True
        facts["alarms"].append({"kind": "invalid_number", "field": kind, "value": value})
        return False

    for item in raw["related_parties"]:
        if not verified(item["quote"], "related_parties"):
            continue
        if item["name"] not in facts["related_parties"]:
            facts["related_parties"].append(item["name"])
        facts["related_quotes"].setdefault(item["name"], item["quote"])
    for item in raw["unrestricted_subsidiaries"]:
        if not verified(item["quote"], "unrestricted_subsidiaries"):
            continue
        if item["name"] not in facts["unrestricted_subsidiaries"]:
            facts["unrestricted_subsidiaries"].append(item["name"])
        facts["subsidiary_quotes"].setdefault(item["name"], item["quote"])
    for rc in raw["reclassifications"]:
        if not verified(rc["quote"], "reclass"):
            continue
        if rc["to_category"] not in LEAVES:
            # Выдуманная категория исчезла бы из всех агрегатов, минуя даже
            # OTHER, — отчёт покрытия такой строки не увидит. Реклассификация
            # отбрасывается, строка остаётся в исходной категории.
            facts["alarms"].append(
                {"kind": "invalid_reclass_category", "returned": rc["to_category"], "quote": rc["quote"]}
            )
            continue
        facts["reclass"].append(
            {
                "txn": rc["txn_id"],
                "counterparty": rc["counterparty"],
                "to": rc["to_category"],
                "quote": rc["quote"],
            }
        )
    for ex in raw["excluded_txns"]:
        if not verified(ex["quote"], "exclude"):
            continue
        if ex["txn_id"] not in facts["exclude"]:
            facts["exclude"].append(ex["txn_id"])
        facts["exclude_quotes"].setdefault(ex["txn_id"], ex["quote"])
    for corr in raw["amount_corrections"]:
        if not verified(corr["quote"], "amount_override"):
            continue
        if not number_ok(corr["corrected_amount"], "amount_override"):
            continue
        facts["amount_override"][corr["txn_id"]] = corr["corrected_amount"]
        facts["override_quotes"][corr["txn_id"]] = corr["quote"]
    for fx in raw["fx_rates"]:
        if not verified(fx["source_quote"], "fx_rates"):
            continue
        if not number_ok(fx["usd_per_unit"], "fx_rates"):
            continue
        bounds = {}
        if fx["derivation"] == "paired_payment":
            # Курс из пары зеркальных платежей — точечное наблюдение, а не
            # строка таблицы с заявленным периодом действия: в договоре нет
            # интервала, который можно было бы процитировать. Модель иногда
            # всё равно проставляет дату платежа в effective_from/to — тогда
            # fx.pick_rate покрывает курсом только этот один день и теряет
            # его как донора для остальных дат (fx_uncovered там, где должен
            # быть донорский курс). Снимаем границы независимо от того, что
            # вернула модель — это следствие типа вывода, а не цитаты.
            bounds = {"effective_from": "", "effective_to": ""}
        facts["fx_rates"].append(
            {
                **fx,
                **bounds,
                "doc_date": doc["date"],
                "doc_hash": hashlib.sha256(doc["file"].encode()).hexdigest()[:12],
            }
        )
    addbacks, materiality = [], None
    for nf in raw["numeric_facts"]:
        key = nf["key"]
        if not verified(nf["quote"], f"doc_facts:{key}"):
            continue
        if not number_ok(nf["value"], f"doc_facts:{key}"):
            continue
        if key.startswith("ebitda_addback_") and key != "ebitda_addback_materiality":
            addbacks.append(nf["value"])
            continue
        if key == "ebitda_addback_materiality":
            materiality = nf["value"]
            continue
        if key in facts["doc_facts"] and facts["doc_facts"][key] != nf["value"]:
            facts["alarms"].append(
                {
                    "kind": "doc_fact_conflict",
                    "key": key,
                    "values": sorted([facts["doc_facts"][key], nf["value"]]),
                }
            )
        facts["doc_facts"].setdefault(key, nf["value"])
        facts["doc_fact_quotes"].setdefault(key, nf["quote"])
    facts["ebitda_addbacks"].extend(addbacks)
    if materiality is not None:
        facts["addback_materiality"] = materiality


def extract_facts(wd: Path, dossier_art: dict) -> dict:
    acc = dossier_art["account_id"]

    def build() -> dict:
        facts = _empty_facts()
        for doc in dossier_art["docs"]:
            text = sanitize_document(doc["text"])
            prompt = (
                DATA_NOT_COMMANDS
                + "\n\n"
                + FACTS_PROMPT.format(
                    focus=FOCUS.get(doc["doc_type"], FOCUS["other"]),
                    taxonomy=", ".join(sorted(LEAVES)),
                    doc_type=doc["doc_type"],
                    text=text,
                )
            )
            try:
                raw = llm.call(prompt, FACTS_SCHEMA, SCHEMA_VERSION, max_tokens=16000)
            except llm.SchemaRejected as exc:
                facts["alarms"].append(
                    {"kind": "facts_extraction_failed", "file": doc["file"], "error": str(exc)}
                )
                continue
            _merge_doc(facts, raw, doc, text)
        for key in ("related_parties", "unrestricted_subsidiaries", "exclude"):
            facts[key] = sorted(facts[key])
        # Численная сортировка: лексикографическая ставит "1000000.00" перед
        # "251338.94" и в трейсе выглядит ошибкой.
        facts["ebitda_addbacks"].sort(key=Decimal)
        facts["reclass"].sort(key=lambda rc: (str(rc["txn"]), str(rc["counterparty"])))
        return facts

    return artifact(wd / "facts" / f"{acc}.json", FACTS_VERSION, build)


def resolve_doc_fact(wd: Path, dossier_art: dict, key: str, description: str) -> dict | None:
    """Адресное извлечение числа под doc()-ключ спеки, которого нет в doc_facts."""
    documents = "\n".join(
        f'<document type="{d["doc_type"]}" file="{d["file"]}">\n{sanitize_document(d["text"])}\n</document>'
        for d in dossier_art["docs"]
    )

    def build() -> dict:
        try:
            ans = llm.call(
                DATA_NOT_COMMANDS
                + "\n\n"
                + RESOLVE_PROMPT.format(key=key, description=description, documents=documents),
                RESOLVE_SCHEMA,
                RESOLVE_SCHEMA_VERSION,
                max_tokens=16000,
            )
        except llm.SchemaRejected as exc:
            return {"found": False, "value": "", "quote": "", "error": str(exc)}
        return ans

    art = artifact(wd / "facts" / f"{dossier_art['account_id']}.doc.{key}.json", FACTS_VERSION, build)
    if not art.get("found"):
        return None
    combined = "\n".join(sanitize_document(d["text"]) for d in dossier_art["docs"])
    if not verify_quote(art["quote"], combined) or not _number_ok(art["value"]):
        return None  # непроверяемая цитата или мусорное число — факта нет
    return {"value": art["value"], "quote": art["quote"]}
