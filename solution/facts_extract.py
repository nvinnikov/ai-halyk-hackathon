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

FACTS_VERSION = 20
# v20 — RESOLVE_METRIC_PROMPT уточнён: условие срабатывания ковенанта
# (порог другого показателя, событие) — не часть вычисляемой величины.
# v19 — грамматика формульного резолва doc-ключа (RESOLVE_METRIC_PROMPT)
# получила фильтр по контрагентам именованными множествами; литеральный
# перечень имён по-прежнему отвергается.
# v18 — обрыв цепочки владения по циклу или предельной глубине виден алярмом
# ownership_chain_broken (ревью задачи 5, раунд 1); расчёт не меняется.
# v17 — промпт таблицы владения просит долю держателя-посредника отдельной
# строкой, даже если она названа не в таблице, а прямым текстом: без неё
# цепочка обрывается на первом звене и эффективная доля не считается.
# v16 — связанность считается по эффективной доле владения (произведению по
# цепочке через held_through), а не по одной строке таблицы KYC.
# v15 — ревью PR #23, одиннадцатая волна: непрочитанное примечание видно
# алярмом, а не только отсутствием ключа.
# v14 — ревью PR #23, десятая волна: неназванный масштаб виден алярмом.
# v13 — ревью PR #23, девятая волна: неназванная валюта видна алярмом,
# неразобранное число не роняет решение о масштабе целиком.
# v12 — ревью PR #23, седьмая волна: масштаб применяется при любом множителе,
# кроме противоречивой пары «тысячи против центов»; расхождение двух групповых
# документов сверяется по значению, а не по записи.
# v11 — ревью PR #23, шестая волна: эвристика масштаба сужена до 10³ с
# центами (иначе отказ), нулевой числитель отсекается наравне с отрицательным.
# v10 — ревью PR #23, пятая волна: читаются единицы сумм примечания (валюта и
# масштаб), гейт деградации досье распространён на адресный резолв.
# v9 — ревью PR #23, вторая волна: group_capex от модели не попадает в
# doc_facts вовсе, а resolve_doc_fact больше не видит документы группового
# уровня ни в промпте, ни в корпусе проверки цитат.
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

OWNERSHIP_SCHEMA_VERSION = "ownership-2"

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
                    "held_through": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["name", "share_percent", "held_through", "quote"],
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

- shares: доли участия — по каждой организации, чья доля голосующих прав
  названа в документе (строкой таблицы участия ИЛИ отдельным предложением вне
  таблицы — например, о доле самой Группы в промежуточной организации, через
  которую держится косвенная доля): организация (name), её доля в процентах
  числом без знака процента (share_percent, строкой), держатель доли
  (held_through — имя организации, ЧЕРЕЗ которую доля удерживается, если
  документ называет её; пустая строка, если доля прямая) и дословная цитата
  (quote). Перемножать доли не нужно: выпиши как напечатано, каждую отдельной
  строкой. Долей в документе нет — пустой список.
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
        "currency": {"type": "string"},
        "amount_scale": {"type": "string"},
        "units_quote": {"type": "string"},
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
        "currency",
        "amount_scale",
        "units_quote",
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

Отдельно — в каких единицах напечатаны ИМЕННО ЭТИ числа:
- currency: код валюты этих сумм (например USD), как он назван в документе;
- amount_scale: множитель, если суммы приведены в кратных единицах — «1000»
  для «в тысячах», «1000000» для «в миллионах». Если суммы напечатаны
  полностью — «1»;
- units_quote: дословная цитата, где единицы названы (шапка примечания или
  строка таблицы). Не нашли — все три строки пустые.

Числа — строкой, без разделителей разрядов. Любое поле, которого в тексте нет,
— пустая строка. Ничего не вычисляй и не пересчитывай сам.

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


_MAX_OWNERSHIP_DEPTH = 4


def _effective_shares(rows: list[dict], alarms: list[dict] | None = None) -> dict[str, Decimal]:
    """Эффективная доля = произведение долей по цепочке владения.

    Таблица KYC даёт рёбра: «доля 52% в T, удерживаемая через Mid» вместе с
    «доля 24% в Mid» означает эффективные 12.48% в T, а не 52%. Сравнение с
    порогом делает код (арифметика), модель только выписывает рёбра.

    Организация может быть названа несколькими строками (прямая доля и
    косвенная) — побеждает БОЛЬШАЯ: связанность возникает от любого из путей,
    и занижение здесь стоило бы выпадения настоящей связанной стороны.
    Неизвестный держатель — строка считается прямой: она раскрыта в другом
    документе, и обнулять её мы права не имеем.

    Цикл и глубина больше _MAX_OWNERSHIP_DEPTH обрываются: это дефект таблицы,
    а не владение — но, в отличие от неизвестного держателя, дефект таблицы
    молчать не должен (соседние защиты в этом файле тоже оставляют след):
    `alarms`, если передан, получает `ownership_chain_broken` с именем
    организации и причиной обрыва (`cycle` / `max_depth`). Обрыв не меняет
    расчёт — он всё так же откатывается к прямой доле строки."""
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        by_name.setdefault(r["name"], []).append(r)

    def resolve(name: str, seen: frozenset, depth: int) -> Decimal:
        best = Decimal(0)
        for r in by_name.get(name, []):
            share = r["share_percent"]
            via = (r.get("held_through") or "").strip()
            if via and via != name and via in by_name:
                if via in seen:
                    if alarms is not None:
                        alarms.append(
                            {
                                "kind": "ownership_chain_broken",
                                "name": name,
                                "held_through": via,
                                "reason": "cycle",
                            }
                        )
                elif depth >= _MAX_OWNERSHIP_DEPTH:
                    if alarms is not None:
                        alarms.append(
                            {
                                "kind": "ownership_chain_broken",
                                "name": name,
                                "held_through": via,
                                "reason": "max_depth",
                            }
                        )
                else:
                    holder = resolve(via, seen | {name}, depth + 1)
                    share = share * holder / Decimal(100)
            best = max(best, share)
        return best

    return {name: resolve(name, frozenset(), 0) for name in sorted(by_name)}


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

    parsed: list[dict] = []
    for item in raw["shares"]:
        share = number_from_quote(item["share_percent"], item["quote"], "ownership_share")
        if share is None:
            continue
        parsed.append({**item, "share_percent": share, "held_through": item.get("held_through", "")})

    # Перемножение долей по цепочке — арифметика, её делает код (_effective_shares);
    # модель только выписывает таблицу как напечатано.
    effective = _effective_shares(parsed, alarms=facts["alarms"])
    above: list[dict] = []
    below: list[dict] = []
    for item in parsed:
        eff = effective.get(item["name"], item["share_percent"])
        if eff != item["share_percent"]:
            facts["alarms"].append(
                {
                    "kind": "ownership_effective_share",
                    "name": item["name"],
                    "direct": str(item["share_percent"]),
                    "effective": str(eff),
                }
            )
        row = {
            **item,
            "share_percent": str(item["share_percent"]),
            "threshold_percent": raw["threshold_percent"],
        }
        (above if eff >= threshold else below).append(row)
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


def _amount_scale(facts: dict, raw: dict, doc: dict, text: str) -> Decimal | None:
    """Множитель сумм примечания; None — считать нельзя.

    Числитель ковенанта приезжает из ЧУЖОЙ отчётности как есть, а знаменатель
    нормализован в валюту расчёта построчно (fx.to_usd). Консолидированная
    отчётность материнской компании — ровно тот документ, где «в тысячах» в
    шапке и функциональная валюта, отличная от валюты расчёта, штатны. Промах
    масштабом в 10³ на max-ковенанте даёт уверенный COMPLIANT, то есть стоит
    статуса ячейки целиком, и проверка числа в цитате его не ловит: число
    напечатано именно так (ревью PR #23, пятая волна).

    Валюта: названа и не совпадает с валютой расчёта — расчёта нет. Пересчитать
    её здесь нечем, курс материнской компании к строкам заёмщика отношения не
    имеет, а молча принять чужую валюту хуже отсутствия ответа.

    Масштаб против дробной части. Шапка «in thousands» относится к таблицам
    отчётности, а примечание рядом печатает полные суммы с центами — ровно так
    устроен документ публичного набора, и множитель из шапки, взятый буквально,
    завысил бы числитель в 10³ на пустом месте. Но обратное рассуждение «есть
    дробная часть — значит масштаб не тот» держится ТОЛЬКО на 10³ с центами: у
    отчётности «в миллионах» одна цифра после запятой — стандартная вёрстка, и
    сумма там значит в миллион раз больше напечатанного (ревью PR #23, шестая
    волна).

    Поэтому исключение сужено до того случая, который оно чинит, и правило
    целиком читается так: множитель НЕ применяется ровно тогда, когда он равен
    10³ и все дробные суммы напечатаны с двумя знаками. Во всех остальных
    сочетаниях масштаб применяется как названо — они не расхождение, а согласие,
    и отказ там снимал бы ключ при подтверждённых цитатой данных, то есть стоил
    бы ровно той ячейки, ради которой сделан весь проход (ревью PR #23, седьмая
    и восьмая волны — сначала правило было слишком доверчивым, потом слишком
    отказным).

    Отказ в этой функции остаётся ровно за двумя случаями: чужая валюта и
    неразобранное число. Прочая защита стоит дальше — в условиях применимости
    тождества и в проверке знака.

    Дробность меряется тем же _normalize_limit, что и сами суммы: иначе исход
    зависел бы от того, поставила ли модель разделители разрядов вопреки
    промпту: с разделителем строка не парсилась Decimal напрямую и считалась
    целой, без него — дробной.
    """
    from fx import BASE_CURRENCY

    currency = raw["currency"].strip().upper()
    if currency and currency != BASE_CURRENCY:
        facts["alarms"].append(
            {"kind": "group_capex_foreign_currency", "file": doc["file"], "currency": currency}
        )
        return None
    if not currency:
        # Валюта не названа — считаем в валюте расчёта, но НЕ молча (ревью PR
        # #23, девятая волна). Это подстановка по умолчанию, и по природе она
        # та же, что курс 1.0 для строки леджера: если отчётность материнской
        # компании в чужой валюте, числитель поедет против знаменателя,
        # нормализованного построчно, и промах в разы даст неверный статус.
        #
        # Отказываться всё же нельзя: на публичном наборе модель валюту не
        # называет, хотя документ её печатает, — отказ стоил бы ровно той
        # ячейки, ради которой сделан весь проход, причём гарантированно и
        # сегодня, против гипотетической чужой валюты на приватном наборе.
        # Поэтому цена вынесена в алярм: счётчик виден в run-report, и в окне
        # по нему видно, на скольких заёмщиках допущение сработало.
        facts["alarms"].append({"kind": "group_capex_currency_unnamed", "file": doc["file"]})
    raw_scale = raw["amount_scale"].strip()
    if not raw_scale:
        # Масштаб не назван — считаем суммы напечатанными полностью, но НЕ молча
        # (ревью PR #23, десятая волна). Допущение той же природы, что и
        # неназванная валюта выше, а цена промаха даже выше: «in thousands» в
        # шапке примечания занижает числитель ровно в 10³, и на max-ковенанте
        # это уверенный COMPLIANT. На публичном наборе модель возвращает пустой
        # масштаб, то есть боевой путь проходит именно здесь — счётчик обязан
        # быть виден в run-report, как и у валюты.
        facts["alarms"].append({"kind": "group_capex_scale_unnamed", "file": doc["file"]})
        return Decimal(1)
    try:
        scale = Decimal(raw_scale.replace(",", "").replace(" ", ""))
    except InvalidOperation:
        facts["alarms"].append({"kind": "invalid_number", "field": "group_capex_scale", "value": raw_scale})
        return None
    if not scale.is_finite() or scale <= 0:
        facts["alarms"].append({"kind": "invalid_number", "field": "group_capex_scale", "value": raw_scale})
        return None
    if scale == 1:
        return scale
    if not verify_quote(raw["units_quote"], text):
        facts["alarms"].append(
            {"kind": "quote_unverified", "field": "group_capex_scale", "file": doc["file"]}
        )
        return None
    amounts = [raw["opening_value"], raw["closing_value"], raw["depreciation"], raw["additions"]]
    digits = []
    for value in amounts:
        if not str(value).strip():
            continue
        d = _fraction_digits(value)
        if d is None:
            # Не разобралось — поле просто не участвует в решении о масштабе, а
            # его судьбу дальше решает number(), где непарсибельность уже
            # обработана мягко (ревью PR #23, девятая волна). Выход отсюда ронял
            # ВЕСЬ расчёт: одиночная запятая ровно с тремя цифрами намеренно не
            # снимается _normalize_limit, и такое `additions` убивало ячейку,
            # хотя остальные три числа целы и тождество отработало бы.
            facts["alarms"].append(
                {"kind": "invalid_number", "field": "group_capex_scale_decision", "value": value}
            )
            continue
        digits.append(d)
    fractional = [d for d in digits if d > 0]
    if scale == _CENTS_SCALE and fractional and all(d >= 2 for d in fractional):
        # Единственная противоречивая пара: «в тысячах» в шапке против центов в
        # самих суммах — множитель к этим числам не относится. Прочие сочетания
        # масштаба и дробной части — согласие, а не спор, и отказ там снимал бы
        # ключ при полностью подтверждённых данных (ревью PR #23, седьмая волна).
        facts["alarms"].append({"kind": "group_capex_scale_ignored", "file": doc["file"], "scale": raw_scale})
        return Decimal(1)
    return scale


# Единственный множитель, при котором дробная часть в сумме доказывает, что
# масштаб к ней не относится: тысячи против центов. Для 10⁶ и выше дробная
# часть — обычная вёрстка, а не противоречие, и такие суммы масштабируются как
# названо.
_CENTS_SCALE = Decimal(1000)


def _fraction_digits(value: str) -> int | None:
    """Сколько знаков после запятой напечатано; None — число не разобрано."""
    from specs_extract import _normalize_limit

    try:
        d = Decimal(_normalize_limit(str(value)))
    except (InvalidOperation, AttributeError):
        return None
    if not d.is_finite():
        return None
    exponent = d.as_tuple().exponent
    if not isinstance(exponent, int):  # NaN/Infinity уже отсеяны is_finite
        return None
    return max(0, -exponent)


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
        if value > 0:
            return True
        # Ноль отсекается вместе с отрицательным (ревью PR #23, шестая волна).
        # Нулевые капитальные затраты Группы за год — не тот ответ, который
        # бывает правдой в консолидированной отчётности; зато это типовой
        # дефолт непонятого поля, и он проходит все прочие гейты насквозь,
        # включая оба условия применимости, если пришёл названным числом.
        # На max-ковенанте нулевой числитель — гарантированный COMPLIANT, то
        # есть ячейка в ноль по статусу.
        facts["alarms"].append({"kind": "group_capex_non_positive", "file": doc["file"], "value": str(value)})
        return False

    scale = _amount_scale(facts, raw, doc, text)
    if scale is None:
        return None

    stated = number(raw["additions"], raw["additions_quote"], "group_capex_additions")
    if stated is not None:
        stated *= scale
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
        # Единственный отказ этой функции, у которого раньше не было имени
        # (ревью PR #23, одиннадцатая волна). number() молчит на ПУСТОМ поле —
        # для additions это законно («числа в тексте нет», дальше тождество), а
        # здесь означает, что примечание не прочитано, и ячейка уходит на
        # лестницу. Без алярма в run-report такой случай читался как «документ
        # привязан и прочитан нормально»: виден только group_doc_attached.
        # На приватном наборе это самая вероятная ветка — у чужой материнской
        # компании примечание свёрстано по-своему, и не найтись может именно
        # стоимость на начало периода.
        facts["alarms"].append(
            {
                "kind": "group_capex_movement_incomplete",
                "file": doc["file"],
                "fields": [
                    field
                    for field, value in (
                        ("opening", opening),
                        ("closing", closing),
                        ("depreciation", depreciation),
                    )
                    if value is None
                ],
            }
        )
        return None
    additions = (closing - opening + abs(depreciation)) * scale
    return (additions, raw["closing_quote"]) if signed_ok(additions) else None


def _plain(value: Decimal) -> str:
    """Значение строкой без экспоненты и без хвостовых нулей.

    Сравнение двух документов группового уровня идёт по ЗНАЧЕНИЮ, а не по
    записи: str(Decimal) сохраняет экспоненту, а умножение на масштаб её
    сдвигает, поэтому «21847000.000» и «21847000» — одно число в двух записях.
    Без нормализации они дали бы ложный group_capex_conflict и сняли бы ключ, а
    в трейсе окна читались бы как разные суммы (ревью PR #23, седьмая волна).
    """
    return format(value.normalize(), "f")


def _apply_group_capex(facts: dict, computed: list[tuple[Decimal, str, str]]) -> None:
    """Посчитанные по документам группового уровня затраты — в doc_facts.

    Два документа группового уровня с РАЗНЫМИ значениями — это не «взять
    посвежее», а признак, что привязан лишний документ: конечная материнская
    компания у группы одна. Молча взятое одно из двух даёт уверенно
    посчитанную не ту величину, а она на max-ковенанте стоит статуса. Ключ
    снимается, ячейка уходит на лестницу — тот же выбор, что и везде в этом
    расчёте: отсутствие ответа честнее неверного.

    Источник у ключа ровно один — этот. Значение, которое модель выписывает в
    numeric_facts (FACTS_PROMPT просит ключ прямо), в doc_facts не попадает
    вовсе: его отсеивает _merge_doc. Поэтому «не посчитали» здесь означает
    «ключа нет», и ячейка уходит на лестницу — а не считается по числу, которое
    модель приняла за капзатраты Группы.
    """
    if not computed:
        # Пояс поверх подтяжек (ревью PR #23, вторая волна): источник закрыт в
        # _merge_doc, но ключ производный, и цена его протечки — статус ячейки,
        # а не точность. Снятие громкое, чтобы протечка не осталась незамеченной.
        if facts["doc_facts"].pop(GROUP_CAPEX_KEY, None) is not None:
            facts["doc_fact_quotes"].pop(GROUP_CAPEX_KEY, None)
            facts["alarms"].append({"kind": "group_capex_stale_key_dropped"})
        return
    distinct = sorted({_plain(value) for value, _quote, _file in computed})
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
    facts["doc_facts"][GROUP_CAPEX_KEY] = _plain(value)
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
        if key == GROUP_CAPEX_KEY:
            # Ключ производный: его считает КОД по отчётности группового уровня
            # (_group_capex), и другого источника у него нет. FACTS_PROMPT
            # просит его у модели прямо, и промпт здесь сознательно не трогают —
            # его текст держит ключи кэша всей кассеты, — поэтому значение
            # отсеивается тут. Пока шаблон считал числитель по леджеру,
            # модельное значение было безвредным; после перевода шаблона на
            # doc(group_capex) оно ИСПОЛНЯЕТСЯ, а модель выписывает сюда
            # порог из цитаты пункта договора, а не капзатраты Группы (ревью
            # PR #23, вторая волна). Значение не теряется молча — оно в алярме.
            facts["alarms"].append(
                {
                    "kind": "group_capex_from_model_ignored",
                    "value": nf["value"],
                    "quote": nf["quote"],
                    "file": doc["file"],
                }
            )
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
    # Деградация ДОСЬЕ запрещает кэш фактов так же, как своя собственная.
    # dossier при транзиентном сбое не ложится на диск, но объект в памяти
    # отдаётся дальше — и факты, собранные по неполному набору документов,
    # закреплялись под FACTS_VERSION без единого алярма и переживали устранение
    # причины. Поймано вживую: офлайн-прогон уронил маршрутизацию на промахе
    # кассеты, досье не закэшировалось, а факты без документа группового уровня
    # — закэшировались, и следующий ЖИВОЙ прогон честно взял их с диска.
    # Ровно то же случится в окне при перезапуске после сбоя сети.
    # Набор берётся из dossier, а не дублируется здесь: свой список уже разъехался
    # с тамошним на issuer_extraction_failed (ревью PR #23, четвёртая волна) —
    # факты закэшировались бы без документа группового уровня и пережили бы
    # починку маршрутизации, то есть ячейка осталась бы на лестнице навсегда.
    # Плоский импорт по месту: модули solution не пакет, dossier фактов не знает.
    from dossier import DEGRADED_KINDS

    dossier_degraded = any(a.get("kind") in DEGRADED_KINDS for a in dossier_art.get("alarms", []))
    return artifact(
        wd / "facts" / f"{acc}.json",
        FACTS_VERSION,
        build,
        cache_if=lambda d: not dossier_degraded
        and not any(a.get("kind") in _degraded_kinds for a in d["alarms"]),
    )


def resolve_doc_fact(wd: Path, dossier_art: dict, key: str, description: str) -> dict | None:
    """Адресное извлечение числа под doc()-ключ спеки, которого нет в doc_facts.

    Периметр тот же, что у общего прохода фактов: документы группового уровня
    сюда не входят. Довод «решения материнской компании не применяются к
    операциям заёмщика» ровно так же относится к её ЧИСЛАМ — обязательство по
    персоналу или страховое покрытие в консолидированной отчётности носят то же
    название и больше на порядок. Защита цитатой это не ловит: цитата
    верифицируется против того же корпуса, поэтому фильтровать надо оба —
    и промпт, и корпус (ревью PR #23, вторая волна).
    """
    own_docs = [d for d in dossier_art["docs"] if d.get("scope") != "group"]
    documents = "\n".join(
        f'<document type="{d["doc_type"]}" file="{d["file"]}">\n{sanitize_document(d["text"])}\n</document>'
        for d in own_docs
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
    # Деградация досье блокирует кэш здесь по той же причине и тем же набором
    # (ревью PR #23, пятая волна): вход у резолва тот же самый — docs досье, —
    # каталог и версия стадии те же, а «числа нет» по неполному набору
    # документов приходит БЕЗ единого алярма (found=false — законный ответ) и
    # переживает устранение причины. Следующий прогон вернул бы found=false с
    # диска: doc_fact_unresolved, ячейка на лестнице до конца окна.
    from dossier import DEGRADED_KINDS

    dossier_degraded = any(a.get("kind") in DEGRADED_KINDS for a in dossier_art.get("alarms", []))
    art = artifact(
        wd / "facts" / f"{dossier_art['account_id']}.doc.{key}.json",
        FACTS_VERSION,
        build,
        cache_if=lambda d: not dossier_degraded and not d.get("alarms"),
    )
    if not art.get("found"):
        return None
    combined = "\n".join(sanitize_document(d["text"]) for d in own_docs)
    if not verify_quote(art["quote"], combined):
        return None  # непроверяемая цитата — факта нет
    # Значение — не всегда чистое число: адресный резолв величины вроде
    # «не более N% of Revenue» честно возвращает её словами, раз в
    # документах она не названа суммой. _number_ok такую строку отсеивает —
    # признаёт её отдельно rewrites.parse_percent_of_statute (тот же разбор,
    # которым потом эту строку подставляет в AST resolve_percent_of_statute).
    # Любая ДРУГАЯ нечисловая строка (порог с суффиксом кратности вроде
    # «x» и т.п.) гейт не проходит и отбрасывается, как и раньше. Плоский
    # импорт по месту: модули solution не пакет, цикла нет (rewrites не
    # импортирует facts_extract).
    import rewrites

    if not _number_ok(art["value"]) and rewrites.parse_percent_of_statute(art["value"]) is None:
        return None  # ни число, ни процентный кэп статьи — мусор, факта нет
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
    # Атрибуция источника для эхо-гарда (ревью пост-мержа PR #26): эхо порога —
    # это число, взятое из текста ДОГОВОРА (обычно прямо из цитаты пункта), а
    # законное равенство порогу живёт в другом документе — полис ровно на
    # требуемую сумму. Оправдание только положительной уликой: цитата
    # верифицируется вне договора И не верифицируется ни в одном договоре.
    # Голое число, живущее в обоих текстах, и цитата, сшитая из нескольких
    # документов (пословная верификация проваливается везде), остаются под
    # подозрением: цена ошибки в эту сторону ограничена статусом (actual на
    # лестнице — тот же порог), обратная — уверенный вердикт впритык.
    # Считается здесь, а не в build(): артефакт резолва не меняется, кэш живёт.
    per_doc = [(d["doc_type"], verify_quote(art["quote"], sanitize_document(d["text"]))) for d in own_docs]
    in_agreement = any(ok for doc_type, ok in per_doc if doc_type == "agreement")
    outside = any(ok for doc_type, ok in per_doc if doc_type != "agreement")
    return {
        "value": art["value"],
        "quote": art["quote"],
        "quote_outside_agreement": outside and not in_agreement,
    }


RESOLVE_METRIC_SCHEMA_VERSION = "docmetric-4"

RESOLVE_METRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "computable": {"type": "boolean"},
        "expression": {"type": "string"},
    },
    "required": ["computable", "expression"],
    "additionalProperties": False,
}

# Категории — из таксономии, не дословным списком: промпт не имеет права
# разъехаться с грамматикой, которая будет валидировать ответ.
RESOLVE_METRIC_PROMPT = """Ниже — пункт кредитного договора. В его формуле встречается величина
«{key}», которой нет числом ни в одном документе: судя по тексту пункта, она
вычисляется из леджера операций заёмщика. Выпиши для неё выражение строго по
грамматике:
  expr    := agg(CATEGORY, sign) | agg(CATEGORY, sign, filters)
           | ratio(a, b) | sub(a, b) | add(a, ...) | max(a, ...) | min(a, ...)
  sign    := in | out | net
  filters := period(YYYY-MM-DD, YYYY-MM-DD) | quarter(n) | desc_contains('строка')
           | counterparty_in(SET)
  SET     := related_parties | unrestricted_subsidiaries
  CATEGORY := {categories}
Фильтр по контрагентам — только через SET из грамматики: если пункт ограничивает
операции с определённой группой контрагентов (связанные стороны, необременённые
дочерние), используй соответствующее имя множества. Перечислять контрагентов
поимённо в выражении нельзя — только эти два названных множества. Если пункт
ограничивает операции с этой группой контрагентов безотносительно статьи (любые
активы, любые платежи, любые операции), категория — ALL, а сама величина всё
равно вычислима: пример формы — agg(ALL, sign, counterparty_in(SET)). Условие
срабатывания ковенанта (порог другого показателя, наступление события) —
не часть этой величины: выражение описывает саму сумму операций, а не
условие, при котором ковенант применяется.
Верни:
- computable=true и expression с выражением — если величина вычислима из
  леджера по тексту пункта;
- computable=false и пустую expression — если величина названа числом в другом
  документе или вообще не выводится из операций.
Числовые константы в выражении запрещены: пороги и любые названные числа не
выписывай. Отвечай строго по тексту пункта, ничего не предполагай.

<document>
{quote}
</document>"""


def resolve_doc_metric(wd: Path, dossier_art: dict, key: str, clause_quote: str) -> str | None:
    """Формульный резолв doc-ключа, вычислимого из леджера; None — отказ.

    Второй ярус после resolve_doc_fact: числа в документах нет, потому что
    величина («выплаты тела долга», «квартальная выручка») — агрегат по
    леджеру, а не документальный факт. LLM читает пункт и выписывает формулу,
    грамматика и таксономия её валидируют, считает код — тот же инвариант,
    что у всего конвейера. Защита от эха порога — конструкцией: Const в
    выражении запрещён и промптом, и проверкой AST, поэтому «значение равно
    порогу» здесь невозможно синтаксически. Doc-узлы запрещены тоже —
    резолв не имеет права ссылаться на другие нерешённые ключи.
    """
    from dsl import Agg, Const, CounterpartyIn, Doc, DslError, parse, uses_ledger, walk
    from taxonomy import ROLLUPS, is_category

    categories = ", ".join(sorted(LEAVES | set(ROLLUPS)))

    def build() -> dict:
        try:
            ans = llm.call(
                DATA_NOT_COMMANDS
                + "\n\n"
                + RESOLVE_METRIC_PROMPT.format(
                    key=key, categories=categories, quote=sanitize_document(clause_quote)
                ),
                RESOLVE_METRIC_SCHEMA,
                RESOLVE_METRIC_SCHEMA_VERSION,
                max_tokens=2000,
            )
        except llm.SchemaRejected as exc:
            # alarms в результате — тот же контракт видимости, что у
            # resolve_doc_fact: провал не кэшируется и виден сканерам.
            return {
                "computable": False,
                "expression": "",
                "alarms": [
                    {
                        "kind": "doc_metric_resolve_failed",
                        "account": dossier_art["account_id"],
                        "key": key,
                        "error": str(exc),
                    }
                ],
            }
        return ans

    from dossier import DEGRADED_KINDS

    dossier_degraded = any(a.get("kind") in DEGRADED_KINDS for a in dossier_art.get("alarms", []))
    art = artifact(
        wd / "facts" / f"{dossier_art['account_id']}.metric.{key}.json",
        FACTS_VERSION,
        build,
        cache_if=lambda d: not dossier_degraded and not d.get("alarms"),
    )
    if not art.get("computable"):
        return None
    expr = str(art.get("expression", ""))
    try:
        node = parse(expr)
    except DslError:
        return None
    if not uses_ledger(node):
        return None
    for n in walk(node):
        if isinstance(n, Doc | Const):
            return None
        if isinstance(n, Agg) and not is_category(n.category):
            return None
        if isinstance(n, CounterpartyIn) and not isinstance(n.setname, str):
            # Литеральный перечень контрагентов — тот же путь, которым в расчёт
            # заезжает выдумка модели; разрешены только именованные множества
            # (грамматика промпта их и предлагает).
            return None
    return expr


# --- определение EBITDA из договора ------------------------------------------

EBITDA_DEF_VERSION = 1
EBITDA_DEF_SCHEMA_VERSION = "ebitda-def-1"

EBITDA_DEF_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "quote": {"type": "string"},
    },
    "required": ["found", "quote"],
    "additionalProperties": False,
}

EBITDA_DEF_PROMPT = """Ниже — кредитный договор. Найди в нём ОПРЕДЕЛЕНИЕ термина
EBITDA (обычно вида «EBITDA означает …» / «EBITDA means …»). Верни:
- found: true, если определение в тексте есть;
- quote: дословный фрагмент текста с определением (одно-два предложения,
  начиная со слова EBITDA), без пересказа и сокращений.
Если определения нет — found: false и пустая строка quote.

<document>
{text}
</document>"""

# Маркеры ШИРОКОГО прочтения («вся операционка» → роллап OPEX_TOTAL). Узкое
# прочтение — дефолт формулировки «за вычетом Операционных расходов» без
# квантора: там «Операционные расходы» — статья отчётности, лист OTHER_OPEX.
# Оба языка на равных правах, как у dsl._SET_STEMS: основы, не полные фразы.
_BROAD_OPEX_MARKERS = (
    "все операционн",
    "всех операционн",
    "всеми операционн",
    "совокупные операционн",
    "совокупных операционн",
    "совокупными операционн",
    "all operating",
    "total operating",
    "aggregate operating",
)


def _classify_ebitda_quote(quote: str) -> str | None:
    """'line_item' | 'all_opex' по цитате определения; None — не классифицируется.

    Классифицирует КОД, не модель: модель только находит определение цитатой.
    None — определение другой природы (например, через «прибыль до налогов»):
    маппинг на категории опекса к нему неприменим, поведение прежнее."""
    norm = " ".join(quote.lower().replace("ё", "е").split())
    if "ebitda" not in norm:
        return None
    if "операционн" not in norm and "operating" not in norm:
        return None
    if any(m in norm for m in _BROAD_OPEX_MARKERS):
        return "all_opex"
    return "line_item"


# Признак разовой корректировки EBITDA (задача 3) — ортогонален выбору
# роллапа опекса выше: договор вправе одновременно сузить статью опекса И
# потребовать учесть разовые статьи, добавленные обратно по согласованию
# аудитора. Это не третье значение reading, а отдельный булев флаг.
#
# Одного слова о «разовости» недостаточно — оно встречается в договорах и по
# другим поводам (разовые платежи, разовые сборы и т.п., не про EBITDA).
# Нужно СОЧЕТАНИЕ: слово о разовом/внеочередном характере статьи И слово о
# самой корректировке или добавлении обратно. Основы, не целые формы — по
# тому же приёму, что _BROAD_OPEX_MARKERS/dsl._SET_STEMS.
#
# «корректир», не «корректировк»: у существительных на «-ка» родительный
# падеж множественного числа теряет «к» перед «-ок» («корректировка» →
# «корректировок»), и более длинный стем эту форму бы не поймал — ровно та
# формулировка встречается в договорах набора («с учётом разовых
# корректировок»).
_ADDBACK_ONE_TIME_STEMS = (
    "разов",
    "внеочеред",
    "one-tim",
    "one-off",
    "nonrecurr",
    "non-recurr",
)
_ADDBACK_ADJUSTMENT_STEMS = (
    "корректир",
    "добавлен",
    "adjustment",
    "add back",
    "add-back",
    "addback",
)


def _quote_requires_addback(quote: str) -> bool:
    """Определение EBITDA прямо разрешает разовую корректировку (addback)?"""
    norm = " ".join((quote or "").lower().replace("ё", "е").split())
    if not any(m in norm for m in _ADDBACK_ONE_TIME_STEMS):
        return False
    return any(m in norm for m in _ADDBACK_ADJUSTMENT_STEMS)


def ebitda_definition(wd: Path, dossier_art: dict) -> dict | None:
    """{'reading': 'line_item'|'all_opex', 'quote': ..., 'needs_addback': bool}
    из договора; None — нет.

    Кейс боевого прогона 2026-08-09: договор пишет «EBITDA означает
    Выручку за вычетом Операционных расходов», а модель извлекла в формулу
    OPEX_TOTAL — роллап всей операционки; EBITDA ушла в минус на сотни
    миллионов, и доля консультационных получила бессмысленный actual. Второе
    прочтение (ebitda_total_opex) легитимно и встречается, поэтому выбор
    делает ТОЛЬКО текст договора: отдельный дешёвый вызов находит определение
    цитатой, цитата верифицируется, классифицирует и применяет код
    (solve._apply_ebitda_reading). Любой сбой — None, поведение прежнее.

    Периметр тот же, что у общего прохода фактов: только договоры самого
    заёмщика, документы группового уровня определение его EBITDA не задают."""
    agreements = [
        d for d in dossier_art["docs"] if d.get("doc_type") == "agreement" and d.get("scope") != "group"
    ]
    if not agreements:
        return None
    text = "\n".join(sanitize_document(d["text"]) for d in agreements)

    def build() -> dict:
        try:
            return llm.call(
                DATA_NOT_COMMANDS + "\n\n" + EBITDA_DEF_PROMPT.format(text=text),
                EBITDA_DEF_SCHEMA,
                EBITDA_DEF_SCHEMA_VERSION,
                max_tokens=4000,
            )
        except llm.SchemaRejected as exc:
            # alarms запрещает кэш (cache_if): отказ схемы не должен пережить
            # рестарт — на повторе ответ мог бы пройти (тот же довод, что у
            # resolve_doc_fact).
            return {
                "found": False,
                "quote": "",
                "alarms": [
                    {
                        "kind": "ebitda_definition_failed",
                        "account": dossier_art["account_id"],
                        "error": str(exc),
                    }
                ],
            }

    # Деградация досье запрещает кэш по тому же доводу, что у резолва: вход —
    # docs досье, «определения нет» по неполному набору документов пришло бы
    # без алярма и пережило бы устранение причины.
    from dossier import DEGRADED_KINDS

    dossier_degraded = any(a.get("kind") in DEGRADED_KINDS for a in dossier_art.get("alarms", []))
    art = artifact(
        wd / "facts" / f"{dossier_art['account_id']}.ebitda_def.json",
        EBITDA_DEF_VERSION,
        build,
        cache_if=lambda d: not dossier_degraded and not d.get("alarms"),
    )
    quote = art.get("quote", "")
    if not art.get("found") or not verify_quote(quote, text):
        return None
    reading = _classify_ebitda_quote(quote)
    if reading is None:
        return None
    return {"reading": reading, "quote": quote, "needs_addback": _quote_requires_addback(quote)}
