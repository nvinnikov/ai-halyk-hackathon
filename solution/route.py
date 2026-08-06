"""Маршрутизация документов (5.2.1): строгая привязка к целевым счетам.

Кандидаты — только целевые account_id из индекса, но ищутся упоминания всех
счетов леджера: документ, указывающий только на нецелевой счёт, — фоновый и
отбрасывается штатно, без алярма и без LLM-вызовов (на приватном наборе
таких большинство). Ноль упоминаний вовсе — карантин с алярмом (счёт может
лежать на слепой странице). Несколько целевых кандидатов — автоматическая
сшивка запрещена, решает LLM с цитатой + алярм.
"""

from pathlib import Path

import llm
from pdftext import doc_hash, extract_pages
from stages import artifact
from vision import read_blind_page

ROUTE_VERSION = 1
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


def route_doc(
    wd: Path, pdf_path: Path, target_accounts: list[str], all_accounts: list[str] | None = None
) -> dict:
    """all_accounts — все счета леджера: по ним распознаются фоновые документы.
    Без них любой документ про чужой счёт выглядел бы как «счетов не найдено»."""

    def build() -> dict:
        text = full_text(wd, pdf_path)
        targets = set(target_accounts)
        known = sorted(set(all_accounts) | targets) if all_accounts else sorted(targets)
        found = [acc for acc in known if acc in text]
        mentions = sorted(a for a in found if a in targets)
        mentions_nontarget = sorted(a for a in found if a not in targets)

        alarms: list[dict] = []
        account, quote, reason = None, "", None
        if len(mentions) == 1:
            account = mentions[0]
        elif len(mentions) > 1:
            alarms.append({"kind": "ambiguous_routing", "candidates": mentions})
            try:
                ans = llm.call(
                    WHOSE_PROMPT.format(candidates=", ".join(mentions), text=text),
                    WHOSE_SCHEMA,
                    WHOSE_SCHEMA_VERSION,
                    max_tokens=4000,
                )
                if ans["account_id"] in mentions:
                    account, quote = ans["account_id"], ans["quote"]
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

        if quarantined:
            # META не вызывается: тип фонового/непривязанного документа никому
            # не нужен, а таких документов большинство — экономия LLM-вызовов.
            meta = {"doc_type": "unrouted", "date": "", "edition": "unmarked"}
        else:
            try:
                meta = llm.call(
                    META_PROMPT.format(text=text), META_SCHEMA, META_SCHEMA_VERSION, max_tokens=4000
                )
            except llm.SchemaRejected:
                meta = {"doc_type": "other", "date": "", "edition": "unmarked"}
                alarms.append({"kind": "meta_extraction_failed", "file": pdf_path.name})
        return {
            "file": pdf_path.name,
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

    return artifact(wd / "route" / f"{doc_hash(pdf_path)}.json", ROUTE_VERSION, build)
