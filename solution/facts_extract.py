"""Факты досье (5.2/5.3): LLM извлекает с цитатами, код сливает детерминированно."""

import hashlib
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import llm
from engine import tokens
from guard import DATA_NOT_COMMANDS, sanitize_document, verify_quote
from stages import artifact
from taxonomy import LEAVES

FACTS_VERSION = 8
# v8 — ревью PR #23: тождество движения стоимости гейтится не только выбытиями,
# но и иными изменениями (обесценение, курсовые разницы); проверка знака стала
# общей для названных и посчитанных поступлений; расхождение двух документов
# группового уровня снимает ключ вместо молчаливого выбора последнего.
# Промпт GROUP_PPE изменён — его единственный ключ кассеты пересчитан.
# v7 — DOSSIER_VERSION=10: в досье появились документы группового уровня
# (scope="group"). Общий проход фактов их НЕ читает — решения материнской
# компании не применяются к операциям заёмщика, — их читает отдельный проход
# GROUP_PPE_PROMPT, и поступления основных средств группы считает код.
# v4 — DOSSIER_VERSION=8: правило недействующих редакций расширено на
# черновики, набор документов снова изменился.
# v3 — досье перестало отдавать замененные редакции кумулятивных типов
# (DOSSIER_VERSION=7): набор документов на входе изменился, и артефакт фактов,
# собранный по старому набору, нёс бы решения из замененного рабочего
# документа. Пересбор бесплатен по LLM: промпт строится на ОДИН документ,
# поэтому выпадение черновика убирает вызов, не меняя ключей остальных.
# v2 — активационный бамп (2026-08-08, docs/ops/activation-step.md): версия
# СОЗНАТЕЛЬНО удерживалась на 1 после смены входа (TEXT_VERSION=2, снят
# футер страницы) и фикса paired_payment в _merge_doc — бамп на исчерпанном
# балансе Anthropic ронял facts_extraction_failed на всех 12 заёмщиках
# (история: docs/ops/debug-extracted-report.md). Поднята на свежей квоте
# Gemini: факты пере-извлечены по исправленному тексту, paired_payment и
# переверификация цитат активны.
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

OWNERSHIP_SCHEMA_VERSION = "ownership-1"

# Отдельный вызов, а не поля в FACTS_SCHEMA: промпт фактов остаётся байт в байт
# тем же, поэтому его ключи кэша не меняются и кассета переживает эту правку.
OWNERSHIP_SCHEMA = {
    "type": "object",
    "properties": {
        "shares": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "share_percent": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["name", "share_percent", "quote"],
                "additionalProperties": False,
            },
        },
        "threshold_percent": {"type": "string"},
        "threshold_quote": {"type": "string"},
    },
    "required": ["shares", "threshold_percent", "threshold_quote"],
    "additionalProperties": False,
}

OWNERSHIP_PROMPT = """Ниже — документ комплаенс-проверки заёмщика. Выпиши из него
две вещи, ничего не сравнивая и не вычисляя:

- shares: таблица участия — организация (name), её доля в процентах числом без
  знака процента (share_percent, строкой) и дословная цитата строки таблицы
  (quote). Если таблицы участия нет — пустой список.
- threshold_percent: доля в процентах, начиная с которой документ признаёт
  организацию связанной стороной, числом строкой; threshold_quote — дословная
  цитата предложения, где этот порог назван. Если порог в документе не назван —
  обе строки пустые.

Не решай, кто связанная сторона: сравнение доли с порогом делается вне модели.

<document type="{doc_type}">
{text}
</document>"""

GROUP_PPE_SCHEMA_VERSION = "group-ppe-1"

# doc()-ключ капитальных затрат Группы. Значение под ним считает КОД (_group_capex);
# то же имя модель знает из FACTS_PROMPT, и её значение код перебивает.
GROUP_CAPEX_KEY = "group_capex"

# Отдельный вызов по тем же соображениям, что OWNERSHIP: промпт фактов остаётся
# байт в байт прежним, ключи его кэша не меняются, кассета переживает правку.
GROUP_PPE_SCHEMA = {
    "type": "object",
    "properties": {
        "opening_value": {"type": "string"},
        "opening_quote": {"type": "string"},
        "closing_value": {"type": "string"},
        "closing_quote": {"type": "string"},
        "depreciation": {"type": "string"},
        "depreciation_quote": {"type": "string"},
        "additions": {"type": "string"},
        "additions_quote": {"type": "string"},
        "no_disposals": {"type": "boolean"},
        "no_disposals_quote": {"type": "string"},
        "other_movements": {"type": "boolean"},
        "other_movements_quote": {"type": "string"},
    },
    "required": [
        "opening_value",
        "opening_quote",
        "closing_value",
        "closing_quote",
        "depreciation",
        "depreciation_quote",
        "additions",
        "additions_quote",
        "no_disposals",
        "no_disposals_quote",
        "other_movements",
        "other_movements_quote",
    ],
    "additionalProperties": False,
}

GROUP_PPE_PROMPT = """Ниже — консолидированная отчётность материнской компании
Группы. Выпиши из примечания об основных средствах то, что в нём НАПЕЧАТАНО,
ничего не вычисляя, не складывая и не выводя одно число из других:

- opening_value: балансовая стоимость основных средств на начало периода;
  opening_quote — дословная цитата строки, где это число напечатано;
- closing_value: та же стоимость на конец периода; closing_quote — цитата;
- depreciation: начисленная за период амортизация основных средств;
  depreciation_quote — цитата;
- additions: поступления (приобретения) основных средств за период, ЕСЛИ
  документ называет их отдельным числом; additions_quote — цитата. Если такого
  числа в тексте нет — обе строки пустые;
- no_disposals: true, только если документ прямо утверждает, что выбытий
  основных средств за период не было; no_disposals_quote — дословная цитата
  этого утверждения. Иначе false и пустая цитата;
- other_movements: true, если примечание называет ЛЮБЫЕ иные изменения
  балансовой стоимости за период, кроме поступлений, выбытий и амортизации —
  обесценение, восстановление обесценения, переоценку, курсовые разницы от
  пересчёта в валюту отчётности, перевод в инвестиционную недвижимость или в
  активы для продажи. Тогда other_movements_quote — дословная цитата строки,
  где такое изменение названо. Если ничего подобного в примечании нет — false и
  пустая цитата.

Числа — строкой, без разделителей разрядов. Любое поле, которого в тексте нет,
— пустая строка. Ничего не вычисляй.

<document type="{doc_type}">
{text}
</document>"""

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
        # is_finite: NaN/Infinity парсятся Decimal'ом без ошибки, но дальше
        # любое сравнение с NaN сигналит InvalidOperation в неожиданном месте
        # (ревью PR #9, 27-я волна — NaN с прогретого артефакта ронял
        # _with_doc_facts на всех заёмщиках).
        return Decimal(value).is_finite()
    except Exception:
        return False


def _percent(value: str) -> Decimal | None:
    """Доля в процентах числом; знак процента и пробелы допускаются."""
    try:
        d = Decimal(value.replace("%", "").replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        return None
    return d if d.is_finite() else None


# Число, стоящее в тексте самостоятельно: соседняя цифра, точка или запятая
# означают, что это кусок другого числа, а не оно само.
_STANDALONE_NUMBER = re.compile(r"(?<![\d.,])\d+(?:[.,]\d+)?(?![\d.,])")


def _percent_in_quote(number: Decimal, quote: str) -> bool:
    """Стоит ли доля в цитате самостоятельным числом, а не куском другого.

    Проверка порогов спек (_limit_in_quote) подстрочна — она обслуживает
    разнообразные формы записи сумм, — и «5» находится внутри «25.0». Для доли
    этого мало: заниженный порог тише завышенного, организации ниже настоящего
    порога молча уезжают в набор с настоящей цитатой, и алярма о снятии в эту
    сторону не бывает.

    Сверяется ЗНАЧЕНИЕ, а не запись. Модель нормализует числа охотнее
    документа — «25» против «25.0%», «31.4» против «31,40%», — и сравнение форм
    ловило бы только укорочение, но не дополнение до вёрстки документа. Отказ
    здесь стоит дорого и несимметрично: порог без значения отключает применение
    кодом целиком и возвращает набор к суждению модели, ради ухода от которого
    правка и делалась.
    """
    return any(_percent(m.group()) == number for m in _STANDALONE_NUMBER.finditer(quote))


def _ownership_rows(facts: dict, raw: dict, doc: dict, text: str) -> tuple[list, list]:
    """Строки таблицы владения, разложенные по порогу: (выше-или-равно, ниже).

    Порог владения применяет код: сравнение доли с порогом — арифметика.

    Модель называет связанные стороны сама, но это решение держится на том, что
    она сделала сравнение (у каждого заёмщика свой порог) — на живом прогоне
    один заёмщик из двенадцати приходил с пустым набором при написанном в
    документе пороге. Здесь модель только выписывает таблицу и порог, а
    принадлежность считается здесь.

    Таблица с порогом старше суждения модели по тем организациям, которые в
    таблице есть: доля ниже порога — не связанная сторона, даже если модель её
    назвала. Организация вне таблицы порогом не отменяется — её связанность
    могла быть раскрыта в другом документе.

    Раскладка отделена от применения (_apply_ownership) намеренно: строки
    собираются по ВСЕМ досье комплаенс-проверки и применяются один раз, иначе
    порядок документов решал бы исход — таблица второго документа снимала бы
    признанное по таблице первого.
    """
    # Проверять существование цитаты мало: число обязано в ней стоять. Тот же
    # инвариант, что у resolve_doc_fact ниже — иначе доля 12.5% из документа
    # приезжает в расчёт как 31.4% при настоящей цитате, и организация молча
    # втягивается в набор (или, что хуже, завышенный порог выкидывает из набора
    # реально связанную сторону и обнуляет ячейку по статусу).
    from specs_extract import _limit_in_quote

    def number_from_quote(value: str, quote: str, field: str) -> Decimal | None:
        number = _percent(value)
        if number is None:
            facts["alarms"].append({"kind": "invalid_number", "field": field, "value": value})
            return None
        if not verify_quote(quote, text):
            facts["alarms"].append({"kind": "quote_unverified", "field": field, "file": doc["file"]})
            return None
        if not _limit_in_quote(str(number), quote) or not _percent_in_quote(number, quote):
            facts["alarms"].append({"kind": "invalid_number", "field": field, "value": value})
            return None
        return number

    if not raw["threshold_percent"]:
        return [], []
    threshold = number_from_quote(raw["threshold_percent"], raw["threshold_quote"], "ownership_threshold")
    if threshold is None:
        return [], []

    above: list[dict] = []
    below: list[dict] = []
    for item in raw["shares"]:
        share = number_from_quote(item["share_percent"], item["quote"], "ownership_share")
        if share is None:
            continue
        row = {**item, "threshold_percent": raw["threshold_percent"]}
        (above if share >= threshold else below).append(row)
    return above, below


def _apply_ownership(facts: dict, above: list[dict], below: list[dict]) -> None:
    """Признанное таблицей — в набор; ниже порога — снять, но не своё же.

    Порядок строк не должен решать исход: организация приходит в таблицу двумя
    строками (прямая доля и косвенная), дублируется в ответе модели или
    встречается в двух досье. Достаточно одной строки не ниже порога, чтобы
    организация была связанной, поэтому строки ниже порога не трогают то, что
    признано таблицей.
    """
    for item in above:
        if item["name"] not in facts["related_parties"]:
            facts["related_parties"].append(item["name"])
        facts["related_quotes"].setdefault(item["name"], item["quote"])

    above_tokens = {tokens(item["name"]) for item in above}
    # Равенство наборов токенов, а не is_related: тот матчит подмножество в обе
    # стороны, и короткое имя из таблицы вычищало бы более длинные чужие — имя
    # из двух слов поглощало бы одноимённую организацию из трёх, раскрытую в
    # другом документе, ровно вопреки обещанию докстринга. Равенство токенов
    # переживает пунктуацию юрформы, ради которой токены и брались.
    for item in below:
        table_tokens = tokens(item["name"])
        if table_tokens in above_tokens:
            continue
        removed = [n for n in facts["related_parties"] if tokens(n) == table_tokens]
        for name in removed:
            facts["related_parties"].remove(name)
            facts["related_quotes"].pop(name, None)
        if removed:
            # Добавление оставляет след в related_quotes, снятие молчало бы:
            # сузившийся набор — это статус ячейки, и в окне прогона надо
            # видеть, кого сняли, по какой доле и против какого порога.
            facts["alarms"].append(
                {
                    "kind": "ownership_below_threshold",
                    "name": item["name"],
                    "share": item["share_percent"],
                    "threshold": item["threshold_percent"],
                    "quote": item["quote"],
                }
            )


def _group_capex(facts: dict, raw: dict, doc: dict, text: str) -> tuple[Decimal, str] | None:
    """Поступления основных средств Группы за период: (значение, цитата).

    Модель здесь только читает. Если документ называет поступления отдельным
    числом — берётся оно. Если нет, они восстанавливаются из движения
    балансовой стоимости: конец − начало + амортизация.

    У этого тождества ДВА условия применимости, и оба проверяются, а не
    подразумеваются. Полное движение стоимости — «начало + поступления −
    выбытия − амортизация − обесценение ± курсовые разницы = конец», и всё, что
    в формулу не вошло, молча уезжает в числитель:

    - выбытий за период не было — иначе выражение даёт поступления за вычетом
      выбывшего;
    - иных изменений стоимости (обесценение, переоценка, курсовые разницы) в
      примечании нет. Консолидация с зарубежными дочками — типовой случай, и
      курсовая разница там не экзотика (ревью PR #23, замечание 3): её знак
      произволен, величина ничем не ограничена, и подмешанная в поступления она
      неотличима от настоящих капитальных затрат.

    Оба признака извлекаются с цитатами и проверяются здесь; не подтвердился
    любой — расчёта нет, и ячейка честно уходит на лестницу. Названные в
    документе поступления обоими условиями не связаны: там читать нечего, число
    напечатано.

    Число обязано стоять в собственной верифицированной цитате — тот же
    инвариант, что у порогов спек и долей владения: цитата привязывает число к
    его формулировке, иначе стоимость на начало приезжает в расчёт как
    стоимость на конец при настоящей цитате.
    """
    from specs_extract import _limit_in_quote, _normalize_limit

    def number(value: str, quote: str, field: str) -> Decimal | None:
        if not str(value).strip():
            return None
        try:
            d = Decimal(_normalize_limit(str(value)))
        except InvalidOperation:
            d = None
        if d is None or not d.is_finite():
            facts["alarms"].append({"kind": "invalid_number", "field": field, "value": value})
            return None
        if not verify_quote(quote, text):
            facts["alarms"].append({"kind": "quote_unverified", "field": field, "file": doc["file"]})
            return None
        if not _limit_in_quote(str(abs(d)), quote):
            facts["alarms"].append({"kind": "invalid_number", "field": field, "value": value})
            return None
        return d

    def signed_ok(value: Decimal) -> bool:
        """Отрицательных поступлений не бывает — ни посчитанных, ни названных.

        Проверка общая для обеих веток (ревью PR #23, замечание 2): у
        вычисленной она ловит перепутанные начало и конец, у названной —
        выбытия, прочитанные как поступления, и просто не то число под
        подписью. Молча пропущенный отрицательный числитель даёт на
        max-ковенанте уверенный COMPLIANT, то есть обнуляет ячейку по статусу,
        а не портит `actual`.
        """
        if value >= 0:
            return True
        facts["alarms"].append({"kind": "group_capex_negative", "file": doc["file"], "value": str(value)})
        return False

    stated = number(raw["additions"], raw["additions_quote"], "group_capex_additions")
    if stated is not None:
        return (stated, raw["additions_quote"]) if signed_ok(stated) else None

    if not raw["no_disposals"] or not verify_quote(raw["no_disposals_quote"], text):
        facts["alarms"].append({"kind": "group_capex_disposals_unconfirmed", "file": doc["file"]})
        return None
    if raw["other_movements"]:
        # Асимметрия с выбытиями намеренная. «Выбытий не было» документ пишет
        # прямо, и цитата этого утверждения проверяема. «Иных движений нет» —
        # утверждение об ОТСУТСТВИИ строки в таблице, и процитировать его
        # нечем: требование цитаты здесь означало бы пустую цитату и отказ
        # считать там, где считать можно. Поэтому цитата обязательна для
        # положительного ответа (модель назвала движение — пусть покажет
        # строку), а гейт стоит на самом факте.
        facts["alarms"].append(
            {
                "kind": "group_capex_other_movements",
                "file": doc["file"],
                "quote": raw["other_movements_quote"],
            }
        )
        return None
    opening = number(raw["opening_value"], raw["opening_quote"], "group_capex_opening")
    closing = number(raw["closing_value"], raw["closing_quote"], "group_capex_closing")
    depreciation = number(raw["depreciation"], raw["depreciation_quote"], "group_capex_depreciation")
    if opening is None or closing is None or depreciation is None:
        return None
    additions = closing - opening + abs(depreciation)
    return (additions, raw["closing_quote"]) if signed_ok(additions) else None


def _apply_group_capex(facts: dict, computed: list[tuple[Decimal, str, str]]) -> None:
    """Посчитанные по документам группового уровня затраты — в doc_facts.

    Два документа группового уровня с РАЗНЫМИ значениями — это не «взять
    посвежее», а признак, что привязан лишний документ: конечная материнская
    компания у группы одна. Молча взятое одно из двух даёт уверенно
    посчитанную не ту величину, а она на max-ковенанте стоит статуса. Ключ
    снимается, ячейка уходит на лестницу — тот же выбор, что и везде в этом
    расчёте: отсутствие ответа честнее неверного.

    Значение модели из numeric_facts договора код перебивает (дисциплина
    _DERIVED_DOC_KEYS), и алярм об этом называет источники своими именами:
    прежнее значение здесь всегда модельное — код пишет ключ только отсюда.
    """
    if not computed:
        return
    distinct = sorted({str(value) for value, _quote, _file in computed})
    if len(distinct) > 1:
        facts["alarms"].append(
            {
                "kind": "group_capex_conflict",
                "values": distinct,
                "files": sorted(file for _v, _q, file in computed),
            }
        )
        facts["doc_facts"].pop(GROUP_CAPEX_KEY, None)
        facts["doc_fact_quotes"].pop(GROUP_CAPEX_KEY, None)
        return
    value, quote, _file = sorted(computed, key=lambda item: item[2])[0]
    from_model = facts["doc_facts"].get(GROUP_CAPEX_KEY)
    if from_model is not None and from_model != str(value):
        facts["alarms"].append(
            {
                "kind": "derived_doc_key_overridden",
                "key": GROUP_CAPEX_KEY,
                "model": from_model,
                "code": str(value),
            }
        )
    facts["doc_facts"][GROUP_CAPEX_KEY] = str(value)
    facts["doc_fact_quotes"][GROUP_CAPEX_KEY] = quote


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
        if not dossier_art["docs"]:
            # Досье без документов — деградация (обычно следствие сбоя выше
            # по конвейеру), а не «фактов нет»: без алярма пустой артефакт
            # ложился бы на диск как успех, невидимый сканерам, и переживал
            # перезапуск (ревью PR #9, 24-я волна — единственная из пяти
            # стадий, где этот случай не был закрыт).
            facts["alarms"].append({"kind": "no_documents", "account": acc})
        for doc in dossier_art["docs"]:
            if doc.get("scope") == "group":
                # Документ группового уровня описывает материнскую компанию, а
                # не заёмщика: его реклассификации, исключения операций и курсы
                # к строкам леджера заёмщика не относятся. Из него читается
                # ровно то, ради чего он привязан, — отдельным проходом ниже.
                continue
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
        # Порог владения — вторым проходом, после всех документов: сначала
        # набор, который назвала модель, затем правило таблицы поверх него.
        # Только досье комплаенс-проверки: раскрытие долей с порогом живёт
        # там, и ограничение держит стоимость на одном вызове за заёмщика.
        # Строки собираются по всем таким документам и применяются один раз —
        # иначе порядок документов решал бы исход.
        all_above: list[dict] = []
        all_below: list[dict] = []
        for doc in dossier_art["docs"]:
            if doc["doc_type"] != "kyc":
                continue
            text = sanitize_document(doc["text"])
            prompt = DATA_NOT_COMMANDS + "\n\n" + OWNERSHIP_PROMPT.format(doc_type=doc["doc_type"], text=text)
            try:
                own = llm.call(prompt, OWNERSHIP_SCHEMA, OWNERSHIP_SCHEMA_VERSION, max_tokens=8000)
            except Exception as exc:
                # Ловим широко, в отличие от общего прохода: этот проход только
                # уточняет уже названный моделью набор, и его падение (бюджет,
                # промах кассеты, сеть) не должно стоить заёмщику
                # реклассификаций, курсов и обязательств. Артефакт при таком
                # алярме не кэшируется — причина транзиентная, перезапуск
                # обязан перепытаться.
                facts["alarms"].append(
                    {"kind": "ownership_extraction_failed", "file": doc["file"], "error": repr(exc)}
                )
                continue
            above, below = _ownership_rows(facts, own, doc, text)
            all_above.extend(above)
            all_below.extend(below)
        _apply_ownership(facts, all_above, all_below)
        # Капитальные затраты Группы — из отчётности группового уровня. Модель
        # выписывает числа примечания с цитатами, арифметику делает код.
        # Посчитанное собирается по ВСЕМ групповым документам и применяется один
        # раз, как строки владения: иначе порядок документов решал бы исход
        # молча — побеждал бы последний по алфавиту (ревью PR #23, замечание 4).
        computed: list[tuple[Decimal, str, str]] = []
        for doc in dossier_art["docs"]:
            if doc.get("scope") != "group":
                continue
            text = sanitize_document(doc["text"])
            prompt = DATA_NOT_COMMANDS + "\n\n" + GROUP_PPE_PROMPT.format(doc_type=doc["doc_type"], text=text)
            try:
                raw = llm.call(prompt, GROUP_PPE_SCHEMA, GROUP_PPE_SCHEMA_VERSION, max_tokens=8000)
            except Exception as exc:
                # Широко, как ownership-проход: этот документ добавочный, и его
                # непрочтение не должно стоить заёмщику остальных фактов.
                facts["alarms"].append(
                    {"kind": "group_capex_extraction_failed", "file": doc["file"], "error": repr(exc)}
                )
                continue
            found = _group_capex(facts, raw, doc, text)
            if found is not None:
                computed.append((found[0], found[1], doc["file"]))
        _apply_group_capex(facts, computed)
        for key in ("related_parties", "unrestricted_subsidiaries", "exclude"):
            facts[key] = sorted(facts[key])
        # Численная сортировка: лексикографическая ставит "1000000.00" перед
        # "251338.94" и в трейсе выглядит ошибкой.
        facts["ebitda_addbacks"].sort(key=Decimal)
        facts["reclass"].sort(key=lambda rc: (str(rc["txn"]), str(rc["counterparty"])))
        # account в каждом алярме — внутри build(), чтобы он лёг НА ДИСК
        # (ревью PR #9, 19-я волна): _alarm_counts, _collect_report_alarms и
        # sanity._stage_alarms читают facts/*.json с диска, обогащение при
        # чтении они не видели — invalid_number у 12 заёмщиков схлопывался
        # глобальным дедупом точных дублей до «1». Артефакты, собранные до
        # этой правки, остаются без account до пересбора (FACTS_VERSION
        # бампает активационная волна).
        facts["alarms"] = [{**a, "account": acc} for a in facts["alarms"]]
        return facts

    # facts_extraction_failed и no_documents не кэшируются (ревью PR #9, 22-я
    # и 24-я волны): иначе провал вызова или пустое досье запекались бы под
    # FACTS_VERSION и переживали перезапуск. Пересбор no_documents бесплатен
    # (LLM не вызывается). Прочие алярмы (invalid_number, doc_fact_conflict) —
    # свойства ответа модели, их кэшировать правильно.
    _degraded_kinds = {
        "facts_extraction_failed",
        "no_documents",
        "ownership_extraction_failed",
        "group_capex_extraction_failed",
    }
    return artifact(
        wd / "facts" / f"{acc}.json",
        FACTS_VERSION,
        build,
        cache_if=lambda d: not any(a.get("kind") in _degraded_kinds for a in d["alarms"]),
    )


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
            # alarms — чтобы провал был ВИДЕН сканерам run-report/sanity
            # (артефакт .doc.<key>.json лежит в facts/ и сканируется наравне
            # с основными; ревью PR #9, 6-я волна): без поля деградация
            # кэшировалась бы молча под версией стадии.
            return {
                "found": False,
                "value": "",
                "quote": "",
                "error": str(exc),
                "alarms": [
                    {
                        "kind": "doc_fact_resolve_failed",
                        "account": dossier_art["account_id"],
                        "key": key,
                        "error": str(exc),
                    }
                ],
            }
        return ans

    # Провал резолва (alarms в результате) не кэшируется — см. extract_facts.
    art = artifact(
        wd / "facts" / f"{dossier_art['account_id']}.doc.{key}.json",
        FACTS_VERSION,
        build,
        cache_if=lambda d: not d.get("alarms"),
    )
    if not art.get("found"):
        return None
    combined = "\n".join(sanitize_document(d["text"]) for d in dossier_art["docs"])
    if not verify_quote(art["quote"], combined) or not _number_ok(art["value"]):
        return None  # непроверяемая цитата или мусорное число — факта нет
    # Число обязано присутствовать в верифицированной цитате (ревью PR #9,
    # 3-я волна): для порогов спек такая проверка есть (_limit_in_quote), а
    # doc()-факт точно так же способен тихо перевернуть вердикт. Плоский
    # импорт по месту: модули solution не пакет, цикла нет.
    from specs_extract import _limit_in_quote

    # Знак — не часть вёрстки: «-1 200 000» в цитате обычно печатается как
    # «минус 1 200 000» или суммой в скобках; сверяем модуль (ревью PR #9,
    # 8-я волна — иначе отрицательные doc-факты, которые RESOLVE_PROMPT сам
    # просит, отбрасывались бы всегда).
    try:
        unsigned = str(abs(Decimal(str(art["value"]))))
    except InvalidOperation:
        unsigned = str(art["value"]).lstrip("-")
    if not _limit_in_quote(unsigned, art["quote"]):
        return None  # число не из цитаты — факта нет
    return {"value": art["value"], "quote": art["quote"]}
