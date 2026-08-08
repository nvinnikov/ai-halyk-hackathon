"""Маршрутизация документов (5.2.1): строгая привязка к целевым счетам.

Кандидаты — только целевые account_id из индекса, но ищутся упоминания всех
счетов леджера: документ, указывающий только на нецелевой счёт, — фоновый и
отбрасывается штатно, без алярма и без LLM-вызовов (на приватном наборе
таких большинство). Ноль упоминаний вовсе — карантин с алярмом (счёт может
лежать на слепой странице). Несколько целевых кандидатов — автоматическая
сшивка запрещена, решает LLM с цитатой + алярм.
"""

import re
from pathlib import Path

import llm
from guard import DATA_NOT_COMMANDS, sanitize_document, verify_quote
from pdftext import doc_hash, extract_pages
from stages import artifact
from vision import read_blind_page

ROUTE_VERSION = 2
# v2 — активационный бамп (2026-08-08, docs/ops/activation-step.md): версия
# удерживалась на 1 после смены входа (TEXT_VERSION=2, снят футер-номер
# страницы), чтобы не жечь исчерпанный баланс Anthropic повторной
# LLM-маршрутизацией всего публичного workdir (история:
# docs/ops/debug-extracted-report.md). Поднята на свежей квоте Gemini —
# META/WHOSE пересчитаны по тексту без футера, версия стадии снова
# согласована с содержимым входа.
META_SCHEMA_VERSION = "route-meta-1"
WHOSE_SCHEMA_VERSION = "route-whose-1"

META_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {
            "type": "string",
            "enum": ["agreement", "audit_report", "financial_notes", "kyc", "treasury_memo", "other"],
        },
        "date": {"type": "string", "pattern": "^(\\d{4}-\\d{2}-\\d{2})?$"},
        "edition": {"type": "string", "enum": ["final", "draft", "superseded", "unmarked"]},
    },
    "required": ["doc_type", "date", "edition"],
    "additionalProperties": False,
}

META_PROMPT = """Ниже — текст финансового документа. Определи:
- doc_type: кредитный договор (agreement), отчёт о согласованных процедурах /
  аудиторский отчёт (audit_report), примечания к финансовой отчётности
  (financial_notes), досье KYC (kyc), служебная записка казначейства
  (treasury_memo), иначе other;
- date: дата документа в формате YYYY-MM-DD, пустая строка если даты нет;
- edition: final — если документ помечен как окончательный/исполнительный
  экземпляр; draft — черновик/промежуточная версия; superseded — помечен как
  заменённый или недействующая редакция; unmarked — пометок нет.
Отвечай строго по тексту, ничего не предполагай.

<document>
{text}
</document>"""

WHOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "account_id": {"type": "string"},
        "quote": {"type": "string"},
    },
    "required": ["account_id", "quote"],
    "additionalProperties": False,
}

WHOSE_PROMPT = """В тексте документа упомянуто несколько номеров счетов: {candidates}.
Чей это документ? Выбери ровно один account_id из списка — счёт заёмщика,
о котором документ, а не вспомогательный/чужой счёт, упомянутый попутно.
В quote приведи дословный фрагмент текста, который это доказывает.

<document>
{text}
</document>"""


def full_text(wd: Path, pdf_path: Path) -> str:
    art = extract_pages(wd, pdf_path)
    chunks = []
    for p in art["pages"]:
        chunks.append(read_blind_page(wd, pdf_path, p["n"]) if p["blind"] else p["text"])
    return "\n".join(chunks)


def first_page_text(wd: Path, pdf_path: Path) -> str:
    """Шапка несёт компанию, счёт, тип и дату: META читает первую страницу,
    не весь документ — пятикратное сокращение токенов маршрутизации."""
    art = extract_pages(wd, pdf_path)
    p = art["pages"][0]
    return read_blind_page(wd, pdf_path, 1) if p["blind"] else p["text"]


def _mentioned(acc: str, text: str) -> bool:
    # Границы слова обязательны: поиск подстроки ложно совпал бы, когда один
    # идентификатор — префикс другого (например, XXX-111 внутри XXX-1112).
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(acc)}(?![A-Za-z0-9])", text) is not None


def route_doc(
    wd: Path, pdf_path: Path, target_accounts: list[str], all_accounts: list[str] | None = None
) -> dict:
    """all_accounts — все счета леджера: по ним распознаются фоновые документы.
    Без них любой документ про чужой счёт выглядел бы как «счетов не найдено»."""

    def build() -> dict:
        text = sanitize_document(full_text(wd, pdf_path))
        targets = set(target_accounts)
        known = sorted(set(all_accounts) | targets) if all_accounts else sorted(targets)
        found = [acc for acc in known if _mentioned(acc, text)]
        mentions = sorted(a for a in found if a in targets)
        mentions_nontarget = sorted(a for a in found if a not in targets)

        alarms: list[dict] = []
        account, quote, reason = None, "", None

        # META — до решения о привязке: методичка комплаенса может содержать
        # целевой счёт, но документ типа other не привязывается вовсе (правка
        # по замеру — отсев по типу, а не только по счёту). Для документов без
        # целевых кандидатов META не вызывается — таких большинство.
        meta = {"doc_type": "unrouted", "date": "", "edition": "unmarked"}
        if mentions:
            try:
                meta = llm.call(
                    DATA_NOT_COMMANDS
                    + "\n\n"
                    + META_PROMPT.format(text=sanitize_document(first_page_text(wd, pdf_path))),
                    META_SCHEMA,
                    META_SCHEMA_VERSION,
                    max_tokens=4000,
                )
            except llm.SchemaRejected:
                meta = {"doc_type": "other", "date": "", "edition": "unmarked"}
                alarms.append({"kind": "meta_extraction_failed", "file": pdf_path.name})

        if mentions and meta["doc_type"] == "other":
            # Не документ клиента — ожидаемый шум, карантин без алярма-паники.
            reason = "non_client_doc_type"
        elif len(mentions) == 1:
            account = mentions[0]
        elif len(mentions) > 1:
            # file внутрь алярма (ревью PR #9, 22-я волна): без пер-документного
            # поля глобальный дедуп точных дублей в run-report/sanity/invariants
            # схлопывал одинаковые расхождения всего архива до «1».
            alarms.append({"kind": "ambiguous_routing", "file": pdf_path.name, "candidates": mentions})
            try:
                ans = llm.call(
                    DATA_NOT_COMMANDS
                    + "\n\n"
                    + WHOSE_PROMPT.format(candidates=", ".join(mentions), text=text),
                    WHOSE_SCHEMA,
                    WHOSE_SCHEMA_VERSION,
                    max_tokens=4000,
                )
                if ans["account_id"] in mentions:
                    # Цитата обязана быть из текста: непроверяемая — это либо
                    # инъекция, либо галлюцинация, кандидат не подтверждён.
                    if verify_quote(ans["quote"], text):
                        account, quote = ans["account_id"], ans["quote"]
                    else:
                        alarms.append(
                            {
                                "kind": "quote_unverified",
                                "file": pdf_path.name,
                                "field": "routing_quote",
                            }
                        )
            except llm.SchemaRejected:
                pass
            if account is None:
                reason = "ambiguous_unresolved"
        if account is None and reason is None:
            if mentions_nontarget:
                # Фоновый документ — штатный отсев, не пробел (спека 5.2).
                reason = "background_document"
            else:
                reason = "no_account_mentions"
                alarms.append({"kind": "routing_quarantine", "file": pdf_path.name})
        quarantined = account is None
        return {
            "file": pdf_path.name,
            # Дубль хеша из имени артефакта: базовые имена во вложенных
            # каталогах могут коллидировать, потребители адресуются хешем.
            "doc_hash": doc_hash(pdf_path),
            "account_id": account,
            "doc_type": meta["doc_type"],
            "date": meta["date"],
            "edition": meta["edition"],
            "mentions": mentions,
            "mentions_nontarget": mentions_nontarget,
            "quarantined": quarantined,
            "quarantine_reason": reason,
            "alarms": alarms,
            "routing_quote": quote,
        }

    # Провал META-вызова (SchemaRejected → doc_type "other" → карантин
    # non_client_doc_type) не кэшируется (ревью PR #9, 23-я волна): залипший
    # так договор = no_agreement в спеках = три ячейки заёмщика на приоре на
    # каждом последующем прогоне. quote_unverified/ambiguous_routing —
    # свойства ответа модели, кэшируются как раньше.
    return artifact(
        wd / "route" / f"{doc_hash(pdf_path)}.json",
        ROUTE_VERSION,
        build,
        cache_if=lambda d: not any(a.get("kind") == "meta_extraction_failed" for a in d["alarms"]),
    )
