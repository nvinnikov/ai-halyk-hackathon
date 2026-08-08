"""Маршрутизация документов (5.2.1): строгая привязка к целевым счетам.

Кандидаты — только целевые account_id из индекса, но ищутся упоминания всех
счетов леджера: документ, указывающий только на нецелевой счёт, — фоновый и
отбрасывается штатно, без алярма и без LLM-вызовов (на приватном наборе
таких большинство). Ноль упоминаний вовсе — карантин с алярмом (счёт может
лежать на слепой странице). Несколько целевых кандидатов — автоматическая
сшивка запрещена, решает LLM с цитатой + алярм.

Второй проход — только для документов, отправленных в карантин по причине
`no_account_mentions`: отчётность группового уровня называет заёмщика по
наименованию компании (сегментное примечание консолидированной отчётности), а
номера счёта не содержит вовсе, и строгая привязка её теряет. Наименование
берётся не регуляркой по языку, а из УЖЕ отмаршрутизированных документов счёта
(borrower_name) — то есть из того, что документный слой про этот счёт уже знает.
Проход узкий намеренно, условий три: одно наименование в тексте, тип из
GROUP_LEVEL_TYPES и отчётность ЧУЖОЙ компании. Тип отсекает объём — внутренние
регламенты заёмщика печатают его наименование в шапке и по имени нашлись бы все
до одного. Издатель отсекает смысл: без него правило читается как «называет
заёмщика и не печатает номер счёта», под что подходит и собственная отчётность
заёмщика, а она в роли группового документа теряет свои реклассификации и
отдаёт свои основные средства за капзатраты Группы.
"""

import re
from pathlib import Path

import llm
from guard import DATA_NOT_COMMANDS, sanitize_document, verify_quote
from pdftext import doc_hash, extract_pages
from stages import artifact
from vision import read_blind_page

ROUTE_VERSION = 6
# v6 — ревью PR #23, третья волна: сравнение издателя с заёмщиком снимает
# многословные юрформы и считает вложенность совпадением; отказ издателя не
# кэшируется. Набор привязанных документов от этого зависит.
# v5 — ревью PR #23: поиск наименования устойчив к переносу строки, добавлены
# требование различающей силы наименования и проверка принадлежности документа
# материнской компании. Набор привязанных документов от этого зависит.
# v4 — второй проход по наименованию заёмщика (route_group_doc): набор
# привязанных документов изменился, артефакты первого прохода пересобираются
# вместе с ним. LLM-вызовы при этом бесплатны — кэш content-addressed по тексту
# промпта, а промпты META/WHOSE не тронуты.
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

BORROWER_SCHEMA_VERSION = "route-borrower-1"

BORROWER_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "quote": {"type": "string"},
    },
    "required": ["name", "quote"],
    "additionalProperties": False,
}

BORROWER_PROMPT = """Ниже — первые страницы документов одного кредитного счёта.
Как в них называется компания-заёмщик? Верни:
- name: её полное наименование ровно в том виде, как оно напечатано в тексте,
  без кавычек и без пояснений;
- quote: дословный фрагмент текста, где эта компания названа заёмщиком.
Наименование банка-кредитора, аудитора, страховщика и прочих сторон не подходит.
Если заёмщик по этим документам не назван — обе строки пустые.

<document>
{text}
</document>"""

# Типы, у которых имеет смысл привязка по наименованию: отчётность группового
# уровня относится к материнской компании, а заёмщика называет в сегментном
# примечании. Договор и досье комплаенс-проверки сюда не входят намеренно —
# счёт в них напечатан, и документ без счёта такого типа скорее чужой.
GROUP_LEVEL_TYPES = frozenset({"financial_notes", "audit_report"})

ISSUER_SCHEMA_VERSION = "route-issuer-1"

ISSUER_SCHEMA = {
    "type": "object",
    "properties": {
        "reporting_entity": {"type": "string"},
        "quote": {"type": "string"},
    },
    "required": ["reporting_entity", "quote"],
    "additionalProperties": False,
}

ISSUER_PROMPT = """Ниже — финансовая отчётность. Определи, ЧЬЯ она:
- reporting_entity: наименование компании, о финансовом положении которой этот
  документ, ровно в том виде, как оно напечатано в тексте. Это компания из
  шапки и из аудиторского заключения, а не та, что упомянута в примечании о
  сегментах или в перечне дочерних организаций;
- quote: дословный фрагмент текста, который это доказывает.
Если определить нельзя — обе строки пустые. Ничего не сравнивай и не выводи.

<document>
{text}
</document>"""

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


def borrower_name(wd: Path, account: str, pdf_paths: list[Path]) -> dict:
    """Наименование заёмщика по его уже отмаршрутизированным документам.

    В промпт идут только первые страницы: наименование печатается в шапке и в
    преамбуле, а полный текст договора — это десятки тысяч знаков на вызов,
    который отвечает одной строкой.

    Наименование обязано присутствовать в тексте дословно, и не только ради
    защиты от выдумки: им дальше ищут заёмщика в ЧУЖОМ документе обычным
    поиском подстроки, и нормализованная моделью форма («АО ...» вместо
    напечатанного «... JSC») не нашлась бы нигде, а могла бы совпасть не там.
    """

    def build() -> dict:
        text = sanitize_document("\n".join(first_page_text(wd, p) for p in pdf_paths))
        if not text.strip():
            return {"account_id": account, "name": "", "quote": "", "alarms": []}
        try:
            ans = llm.call(
                DATA_NOT_COMMANDS + "\n\n" + BORROWER_PROMPT.format(text=text),
                BORROWER_SCHEMA,
                BORROWER_SCHEMA_VERSION,
                max_tokens=4000,
            )
        except Exception as exc:
            # Широко, как ownership-проход в facts_extract: этот вызов только
            # добавляет документы, и его падение (бюджет, сеть, промах кассеты)
            # не имеет права уронить маршрутизацию, которая уже отработала.
            # Печать обязательна: самый вероятный отказ нового прохода — бюджет,
            # и без строки ALARM он не оставлял следа в stdout (ревью PR #23,
            # вторая волна). Артефакт при этом алярме не кэшируется.
            print(f"ALARM borrower_name_failed {account}: {exc!r}", flush=True)
            return {
                "account_id": account,
                "name": "",
                "quote": "",
                "alarms": [{"kind": "borrower_name_failed", "account": account, "error": repr(exc)}],
            }
        name = ans["name"].strip()
        if not name:
            return {"account_id": account, "name": "", "quote": "", "alarms": []}
        if not verify_quote(ans["quote"], text) or not verify_quote(name, text):
            return {
                "account_id": account,
                "name": "",
                "quote": "",
                "alarms": [{"kind": "quote_unverified", "account": account, "field": "borrower_name"}],
            }
        if not _name_specific_enough(name):
            # Различающая сила наименования — не вкус, а условие применимости:
            # им ищут заёмщика в произвольных чужих документах. Общее слово
            # проходит verify_quote по собственным страницам заёмщика и потом
            # совпадает где угодно (ревью PR #23, вторая волна).
            return {
                "account_id": account,
                "name": "",
                "quote": "",
                "alarms": [{"kind": "borrower_name_too_generic", "account": account, "name": name}],
            }
        return {"account_id": account, "name": name, "quote": ans["quote"], "alarms": []}

    # Версия — ROUTE_VERSION, своего рычага у стадии нет (ревью PR #23, вторая
    # волна): вход артефакта — набор смаршрутизированных на счёт документов и
    # текст их первых страниц, то есть ровно то, чем управляют ROUTE_VERSION и
    # TEXT_VERSION. Независимый счётчик разъехался бы с ними при первой правке
    # маршрутизации и отдал бы наименование, посчитанное по другому набору.
    return artifact(
        wd / "borrower" / f"{account}.json",
        ROUTE_VERSION,
        build,
        cache_if=lambda d: not d["alarms"],
    )


def _name_mentioned(name: str, text: str) -> bool:
    """Наименование как самостоятельная единица текста, а не кусок другого.

    Границы обязательны по той же причине, что у номера счёта: наименования
    заёмщиков в наборе различаются одним словом, и поиск подстрокой втянул бы
    документ соседа.

    Пробел внутри наименования — ЛЮБОЙ пробельный разрыв. Наименование пришло
    из borrower_name, где его пропустил verify_quote, а тот пробелы схлопывает;
    поиск литералом с одиночными пробелами разошёлся бы с той проверкой,
    которая наименование допустила. В вёрстке PDF наименование переносится на
    другую строку прямо посреди слов, и промах здесь молчит по построению —
    документ просто не привязывается (ревью PR #23, вторая волна)."""
    body = r"\s+".join(re.escape(w) for w in name.split())
    return re.search(rf"(?<!\w){body}(?!\w)", text, re.IGNORECASE) is not None


# Наименованием ищут заёмщика в ЧУЖИХ документах, поэтому оно обязано быть
# различающим. Одно общее слово («Группа», «Холдинг», имя города) прошло бы
# verify_quote по собственным страницам заёмщика и затем совпало бы где угодно;
# два значимых слова — минимум, при котором совпадение что-то значит.
_MIN_NAME_WORDS = 2
_MIN_NAME_CHARS = 8


def _name_specific_enough(name: str) -> bool:
    return len(name) >= _MIN_NAME_CHARS and len([w for w in name.split() if len(w) > 1]) >= _MIN_NAME_WORDS


# Многословные юрформы. engine.tokens снимает только односложные (llp/jsc/тоо),
# а шапка договора и шапка отчёта пишут одну и ту же компанию по-разному:
# «... Services JSC» против «... Services Joint Stock Company». Несовпадение
# наборов токенов здесь означало бы «издатель чужой», то есть привязку
# собственной отчётности заёмщика как групповой (ревью PR #23, третья волна).
_LONG_LEGAL_FORMS = (
    "public joint stock company",
    "joint stock company",
    "limited liability partnership",
    "limited liability company",
    "открытое акционерное общество",
    "закрытое акционерное общество",
    "публичное акционерное общество",
    "акционерное общество",
    "товарищество с ограниченной ответственностью",
    "общество с ограниченной ответственностью",
)


def _entity_key(name: str) -> frozenset[str]:
    """Токены наименования без юрформ — и односложных, и многословных."""
    from engine import tokens

    stripped = name.lower()
    for form in _LONG_LEGAL_FORMS:
        stripped = stripped.replace(form, " ")
    return tokens(stripped)


def _same_entity(issuer: str, own_name: str) -> bool:
    """Одна ли это компания. Сомнение трактуется как «да».

    Сравнение несимметрично по цене, поэтому и правило несимметрично. Ложное
    «чужая» привязывает собственную отчётность заёмщика как групповую: она
    теряет свои реклассификации и курсы (общий проход групповые документы не
    читает) и отдаёт свои основные средства за капзатраты Группы — несколько
    ячеек. Ложное «своя» стоит одной ячейки, той самой, ради которой проход и
    делается. Поэтому совпадением считается не только равенство наборов, но и
    вложенность в любую сторону: «X» против «X Holding» — это ровно тот случай,
    где по названию не отличить материнскую компанию от переименования самого
    заёмщика, и решать его угадыванием нельзя.
    """
    a, b = _entity_key(issuer), _entity_key(own_name)
    if not a or not b:
        return True  # опознать нечем — считаем своей и не привязываем
    return a <= b or b <= a


def _issued_by_parent(wd: Path, pdf_path: Path, text: str, own_name: str) -> tuple[bool, list[dict]]:
    """Отчётность ли это ЧУЖОЙ компании, в которую заёмщик входит.

    Без этой проверки условия привязки читаются так: «документ называет
    заёмщика и не печатает номер счёта». Под них подходит и собственная
    отчётность заёмщика, у которой номера счёта просто нет, — а она приезжала
    бы в досье со `scope="group"`, то есть её реклассификации и курсы молча
    выпадали бы из расчёта (общий проход фактов групповые документы не читает),
    а её же основные средства становились бы «капзатратами Группы» (ревью PR
    #23, вторая волна).

    Модель называет компанию, чья это отчётность; СРАВНИВАЕТ код — это ровно
    тот случай, где сравнение делать модели незачем. Правило сравнения и его
    несимметричность — в _same_entity.

    Вызов идёт только по кандидатам, прошедшим и наименование, и тип: на
    публичном наборе это один документ из двухсот.
    """
    try:
        ans = llm.call(
            DATA_NOT_COMMANDS + "\n\n" + ISSUER_PROMPT.format(text=text),
            ISSUER_SCHEMA,
            ISSUER_SCHEMA_VERSION,
            max_tokens=4000,
        )
    except llm.SchemaRejected:
        # Не опознали издателя — не привязываем: пропуск документа стоит одной
        # ячейки, а ошибочная привязка собственной отчётности заёмщика роняет
        # его реклассификации и курсы, то есть несколько.
        return False, [{"kind": "issuer_extraction_failed", "file": pdf_path.name}]
    issuer = ans["reporting_entity"].strip()
    if not issuer or not verify_quote(ans["quote"], text) or not verify_quote(issuer, text):
        return False, [{"kind": "quote_unverified", "file": pdf_path.name, "field": "reporting_entity"}]
    if _same_entity(issuer, own_name):
        return False, [{"kind": "own_reporting_rejected", "file": pdf_path.name, "entity": issuer}]
    return True, []


def route_group_doc(wd: Path, pdf_path: Path, names: list[tuple[str, str]]) -> dict:
    """Привязка документа группового уровня к заёмщику по его наименованию.

    Зовётся только для документов, которые первый проход отправил в карантин с
    `no_account_mentions`. Условий ТРИ и все обязательны: ровно одно
    наименование из пула найдено в тексте (два — сшивка запрещена, как у
    номеров счетов), META относит документ к отчётности, и отчётность эта —
    ЧУЖОЙ компании, а не самого заёмщика (`_issued_by_parent`).

    Второе условие несёт основную нагрузку по объёму: наименование заёмщика
    стоит в шапке каждого его внутреннего регламента, и без отсева по типу
    проход втянул бы в досье весь делопроизводственный шум набора. Третье —
    по смыслу: без него правило читается как «называет заёмщика и не печатает
    номер счёта», под что подходит и собственная отчётность заёмщика.

    Отказ здесь молчит: документ уже лежит в карантине с алярмом первого
    прохода, и второй алярм на тот же файл только размыл бы
    `target_doc_dropped_as_other` — единственный сигнал о том, что документный
    слой ослеп на класс документов."""

    def build() -> dict:
        text = sanitize_document(full_text(wd, pdf_path))
        hits = sorted(acc for acc, name in names if _name_mentioned(name, text))
        alarms: list[dict] = []
        account: str | None = None
        reason: str | None = "no_named_borrower"
        meta = {"doc_type": "unrouted", "date": "", "edition": "unmarked"}
        if len(hits) > 1:
            reason = "ambiguous_named_borrowers"
            alarms.append({"kind": "ambiguous_name_routing", "file": pdf_path.name, "candidates": hits})
        elif len(hits) == 1:
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
            if meta["doc_type"] not in GROUP_LEVEL_TYPES:
                reason = "named_doc_not_group_level"
            else:
                own_name = next(name for acc, name in names if acc == hits[0])
                verdict, issuer_alarms = _issued_by_parent(wd, pdf_path, text, own_name)
                alarms.extend(issuer_alarms)
                if verdict:
                    account, reason = hits[0], None
                    # Привязка по имени — исключение из строгого правила, и она
                    # обязана быть видна поимённо: на приватном наборе это
                    # список документов, которых не должно было быть в досье.
                    alarms.append({"kind": "group_doc_attached", "file": pdf_path.name, "account": hits[0]})
                else:
                    reason = "own_reporting_not_group_level"
        return {
            "file": pdf_path.name,
            "doc_hash": doc_hash(pdf_path),
            "account_id": account,
            "doc_type": meta["doc_type"],
            "date": meta["date"],
            "edition": meta["edition"],
            "mentions": [],
            "mentions_nontarget": [],
            "quarantined": account is None,
            "quarantine_reason": reason,
            "alarms": alarms,
            "routing_quote": "",
        }

    # Ключ артефакта — хеш документа; пул наименований зависит от датасета, а
    # каталог work/ уже разделён по dataset_hash. Провал META не кэшируется по
    # той же причине, что в route_doc.
    return artifact(
        wd / "route_group" / f"{doc_hash(pdf_path)}.json",
        ROUTE_VERSION,
        build,
        # issuer_extraction_failed наравне с META (ревью PR #23, третья волна):
        # природа сбоя та же — ответ не прошёл валидацию, в LLM-кэш не лёг, и на
        # повторе прошёл бы. Артефакт route_group инвалидируется только по
        # ROUTE_VERSION, поэтому закэшированный отказ издателя был бы
        # неустраним, а на нём висит ровно та ячейка, ради которой сделан проход.
        cache_if=lambda d: not any(
            a.get("kind") in ("meta_extraction_failed", "issuer_extraction_failed") for a in d["alarms"]
        ),
    )


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
            # Но документ НАЗЫВАЕТ целевой счёт и всё равно выбрасывается, а
            # перечень типов закрыт теми пятью, что встретились на публичном
            # наборе: допсоглашение, сертификат соблюдения ковенантов, решение
            # совета, полис на приватном наборе лягут сюда же и исчезнут. Тип
            # определяется по первой странице — маркер на второй документ тоже
            # не спасёт. Поведение не меняем (карантин остаётся), но молчать
            # нельзя: в окне это единственный способ увидеть, что документный
            # слой ослеп на целый класс документов.
            reason = "non_client_doc_type"
            alarms.append(
                {"kind": "target_doc_dropped_as_other", "file": pdf_path.name, "accounts": mentions}
            )
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
