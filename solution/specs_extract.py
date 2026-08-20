"""Пункт → спека (5.3): LLM читает договор, грамматика проверяет до исполнения.

quote обязателен: он и есть трейс, и по нему верификатор (и эвристика
лестницы) работают, не перечитывая PDF. Артефакт на диске хранит только
сырой ответ модели (covenants + алярмы извлечения) — грамматическая и
guard-проверка (_check) гоняются заново при каждом вызове extract_specs
с актуальными fact_keys, а не однократно при извлечении: подрезка фактов
после спек не должна требовать повторного похода к модели.
"""

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import llm
from dsl import Cmp, Doc, DslError, parse, validate, walk
from fallbacks import family_of
from guard import DATA_NOT_COMMANDS, sanitize_document, verify_quote
from stages import artifact
from taxonomy import LEAVES
from templates import match_signature, title_key

# v2 — активационный бамп (2026-08-08): артефакт стадии кэшируется по версии,
# а не по промпту — правки SPECS_PROMPT (пример «7% => 0.07») и
# DATA_NOT_COMMANDS без бампа остались бы неактивными на прогретом workdir
# (сырой ответ старого промпта продолжал бы отдаваться из specs/*.json).
SPECS_STAGE_VERSION = 2
SCHEMA_VERSION = "specs-1"

# Порог семейного выброса (задача 23, п.5в): порог отличается от медианы
# порогов той же семьи метрики в этом прогоне на порядок и более.
_OUTLIER_FACTOR = Decimal(10)

_CLAUSE_NUM = re.compile(r"\d+(?:\.\d+)*")

SPECS_SCHEMA = {
    "type": "object",
    "properties": {
        "covenants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clause": {"type": "string"},
                    "quote": {"type": "string"},
                    "metric": {"type": "string"},
                    "direction": {"type": "string", "enum": ["max", "min"]},
                    "limit": {"type": "string"},
                    "trigger": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                },
                "required": ["clause", "quote", "metric", "direction", "limit", "trigger", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["covenants"],
    "additionalProperties": False,
}

SPECS_PROMPT = """Ниже — кредитный договор. Найди в нём ВСЕ финансовые ковенанты
(обязательства с числовым порогом) и для каждого выдай:
- clause: номер пункта, под которым ковенант напечатан в договоре;
- quote: дословная цитата пункта;
- metric: показатель на DSL (грамматика ниже);
- direction: max — показатель не должен превышать порог, min — не должен быть ниже;
- limit: порог строкой (доли — числом: 7% => 0.07; кратности: 2.5x => 2.5);
- trigger: если тест применяется только при условии — условие как сравнение
  gt/ge/lt/le двух DSL-выражений, иначе null. trigger заполняется ТОЛЬКО когда
  применение теста зависит от значения показателя (пример: «если поступления
  по финансированию превышают пороговую сумму» — springing-ковенант). Срок действия
  договора, отчётный период, дата — НЕ триггеры, для них trigger: null;
- confidence: уверенность 0..1.

Грамматика DSL:
  expr    := agg(category, sign, filters?) | doc(key) | ratio(a,b) | sub(a,b)
           | add(a...) | max(a...) | min(a...) | const(x)
  sign    := out | in | net   (out — расходы по модулю, дефолт для расходов;
                               net — с неттингом сторно, только если договор явно
                               требует вычитать возвраты/сторно из расхода;
                               in — поступления)
  filters := period(YYYY-MM-DD,YYYY-MM-DD) | quarter(n)
           | counterparty_in(related_parties | unrestricted_subsidiaries | ['Имя', ...])
           | txn_in(['TXN', ...]) | min_amount(x) | desc_contains('строка')
  ВНИМАНИЕ: period()/quarter() — это ОТЧЁТНЫЙ период метрики (за какой интервал
  считать), НЕ исключение операций. Исключение конкретной операции по
  документальному решению (отсечение периода, переход рисков) НЕ выражается
  фильтром — оно приходит из фактов досье (excluded_txns) и применяется до DSL.
Категории: {categories}
Роллапы: OPEX_TOTAL (все операционные расходы), ALL (все категории).
EBITDA выражай через sub(agg(REVENUE, in), agg(<роллап>, out)) и выбирай роллап
по тексту договора: OPEX_TOTAL — если договор понимает под операционными
расходами все статьи, OTHER_OPEX — если только прочие/эксплуатационные;
цитируй формулировку, из которой следует выбор.
Если число берётся из документа, а не из леджера (например консолидированный
показатель группы или зафиксированное обязательство) — используй doc(ключ);
доступные ключи: {fact_keys}; если нужного ключа нет — придумай осмысленный
snake_case ключ, он будет извлечён отдельно.

<agreement>
{text}
</agreement>"""


def _normalize_clause(raw: str) -> tuple[str, bool]:
    """Номер пункта из ответа модели то голый, то с префиксом «Пункт»/«п.»/
    «Article» — ключ ячейки берётся только из цифровой части.

    Второй элемент — found: цифровая часть вообще нашлась в ответе модели
    (не «клауза совпала с шаблоном» — совпадение с шаблоном не отсюда)."""
    m = _CLAUSE_NUM.search(raw)
    if m:
        return m.group(), True
    return raw, False


def _strip_trailing_zeros(s: str) -> str:
    return s.rstrip("0").rstrip(".") if "." in s else s


def _limit_forms(limit: str) -> set[str]:
    """Цифровые формы порога для поиска внутри цитаты (два знака, один, целое),
    для долей — процентная форма; каждая форма — ещё и с десятичной запятой и
    с пробелом перед знаком процента (ревью PR #9, 5-я волна: «1,44» и «4 %»
    на другой вёрстке договора не должны стоить ячейку)."""
    try:
        d = Decimal(limit)
    except InvalidOperation:
        return {limit}
    plain = format(d, "f")
    forms = {limit, plain, _strip_trailing_zeros(plain)}
    if 0 < d <= 1:
        pct = _strip_trailing_zeros(format(d * 100, "f"))
        forms |= {f"{pct}%", f"{pct} %"}
    # Десятичная запятая: вариант каждой формы с "," вместо "." (и «4,5 %»).
    forms |= {f.replace(".", ",") for f in forms if "." in f}
    return forms


_THOUSANDS_SEP = re.compile(r"(?<=\d)[,\x20\xa0\u202f](?=\d{3}(?:\D|$))")

# Суффикс кратности после числа: латинский x, кириллический х, знак ×.
_MULT_SUFFIX = re.compile(r"(?<=\d)\s*[x\u0445\u00d7]$", re.IGNORECASE)
# Снимается безусловно только $: суммы кейса нормализованы в USD (fx.py), и
# знак чужой валюты в пороге — единственный сигнал, что порог не в базовой
# валюте. Порог с €/£/₸ остаётся непарсибельным и громко падает в
# invalid_spec, как до правки (ревью PR #11, раунд 2 — молча снятый знак
# давал бы уверенно неверный вердикт с ошибкой в сотни раз).
_CURRENCY_OR_SPACE = re.compile(r"[$\s\xa0\u202f]")
# Только запятая-разряд (пробелы к этому моменту уже сняты _CURRENCY_OR_SPACE).
_COMMA_GROUP = re.compile(r"(?<=\d),(?=\d{3}(?:\D|$))")


def _normalize_limit(raw: str) -> str:
    """Порог из вёрстки модели — в числовую строку: '$1,234,567.89' →
    '1234567.89', '2.5x' → '2.5', '1,44' → '1.44' (живые прогоны Gemini,
    task-28: Decimal падал на валютной форме, спека помечалась invalid и
    ячейка уезжала на лестницу при здоровой цитате и метрике).

    Разрядная запятая снимается только там, где форма однозначна: запятых
    две и больше или в строке уже есть точка-десятичный разделитель.
    Одиночная запятая с 1–2 цифрами после — десятичная ('1,44' → '1.44');
    одиночная с ровно тремя ('0,075' / '7,500') неотличима от разрядной —
    остаётся как есть и громко падает в invalid_spec, как до правки
    (ревью PR #11: '0,075' → '0075' == 75 молча завышал бы порог в 1000
    раз, и _limit_in_quote этого не ловит — цитату калечит тот же
    _degroup_thousands). Непарсибельный порог обязан дойти до _check и
    стать invalid_spec, а не молча исчезнуть."""
    s = _MULT_SUFFIX.sub("", str(raw).strip())
    s = _CURRENCY_OR_SPACE.sub("", s)
    if "." in s or s.count(",") >= 2:
        return _COMMA_GROUP.sub("", s)
    return re.sub(r"(?<=\d),(?=\d{1,2}$)", ".", s)


def _degroup_thousands(text: str) -> str:
    """Снимает разделители тысяч (запятая, неразрывный/узкий неразрывный
    пробел) между группами цифр в числовых значениях. Цитата уже
    прошла verify_quote (доказанно реальный текст договора) — здесь только
    сверяем цифровую форму порога, снимать разделители безопасно."""
    return _THOUSANDS_SEP.sub("", text)


# Масштабные вёрстки порога: «10 млн», «1.5 million», «2 млрд» (ревью PR #9,
# 6-я волна). Слова языкозависимы, но деградация мягкая: не совпало — работают
# обычные цифровые формы, провал всех форм отправляет ячейку на лестницу.
_SCALE_WORDS = {
    Decimal(1_000): r"(?:тыс\.?|thousand|k)",
    Decimal(1_000_000): r"(?:млн\.?|million|mln|m)",
    Decimal(1_000_000_000): r"(?:млрд\.?|billion|bn|b)",
}


def _scaled_in_quote(d: Decimal, quote: str) -> bool:
    for scale, words in _SCALE_WORDS.items():
        q = d / scale
        # «1.5 million» тоже легитимен: множитель с точностью до сотых.
        if q != q.quantize(Decimal("0.01")) or q >= scale:
            continue
        base = _strip_trailing_zeros(format(q, "f"))
        for num in {base, base.replace(".", ",")}:
            if re.search(re.escape(num) + r"\s?" + words + r"(?![a-zа-я])", quote, re.IGNORECASE):
                return True
    return False


# Словесная форма процента: «7 (семи) процентов», "7 percent" — знак %
# в такой вёрстке не печатается (ревью PR #9, 27-я волна; тот же класс,
# что запятая-десятичная и «млн»). Допускается вставка прописью в скобках.
_PERCENT_WORD = re.compile(r"\s*(?:\([^)]{0,60}\))?\s*(?:процент|percent)", re.IGNORECASE)


def _percent_word_in_quote(d: Decimal, quote: str) -> bool:
    if not 0 < d <= 1:
        return False
    pct = _strip_trailing_zeros(format(d * 100, "f"))
    for p in {pct, pct.replace(".", ",")}:
        for m in re.finditer(rf"(?<![\d.,]){re.escape(p)}(?![\d.,])", quote):
            if _PERCENT_WORD.match(quote[m.end() :]):
                return True
    return False


def _limit_in_quote(limit: str, quote: str) -> bool:
    degrouped = _degroup_thousands(quote)
    if any(form in quote or form in degrouped for form in _limit_forms(limit)):
        return True
    try:
        d = Decimal(limit)
    except InvalidOperation:
        return False
    return _scaled_in_quote(d, quote) or _percent_word_in_quote(d, quote)


# Форма порога, читаемая из ЦИТАТЫ (правило команды со 2-го места): форма
# лимита в тексте определяет форму метрики, а не наоборот. Три текстовых
# признака у числа порога:
#   - суффикс кратности сразу после числа (латинское/кириллическое x/х/×,
#     слово о кратности «раз»/«крат») => метрика обязана быть отношением;
#   - знак или название валюты рядом с числом => метрика обязана быть суммой;
#   - знак процента сразу после числа => метрика обязана быть отношением.
# Про запас — сама конструкция намеренно консервативна: признак ищется
# ВПЛОТНУЮ к числу (не в произвольном месте цитаты), а разные вхождения с
# разными формами дают неоднозначность, а не выбор (см. _threshold_form).
_MULT_SUFFIX_CTX = re.compile(r"^\s*(?:[xх×](?![a-zа-я])|раз[а-я]*\b|кратн\w*)", re.IGNORECASE)
_PERCENT_SUFFIX_CTX = re.compile(r"^\s*%")
_CURRENCY_MARK = r"[$€£¥₸₽₹]"
_CURRENCY_WORD = r"(?:доллар\w*|тенге|евро\w*|рубл\w*|фунт\w*|юан\w*|dollars?|euros?|pounds?|tenge|yuan)"
_CURRENCY_PREFIX_CTX = re.compile(_CURRENCY_MARK + r"\s*$")
_CURRENCY_SUFFIX_CTX = re.compile(r"^\s*(?:" + _CURRENCY_MARK + "|" + _CURRENCY_WORD + ")", re.IGNORECASE)

# Окно контекста после числа: достаточно для «долларов США» / «процентов»,
# но не настолько широкое, чтобы подцепить соседнее число другого пункта.
_SHAPE_CONTEXT_AFTER = 24
_SHAPE_CONTEXT_BEFORE = 4

# Граница числа: символ вплотную до/после найденного вхождения формы не
# обязан быть цифрой, десятичной точкой/запятой или разделителем групп
# тысяч (nbsp/узкий nbsp) — иначе короткий или круглый порог совпадает как
# ПОДСТРОКА внутри чужого числа («1» внутри «21x», «5» внутри «15%») и
# наследует его окружение. Ревью раунд 1: это давало уверенную чужую форму
# вместо отказа — ровно противоположность замыслу правила. Обычный пробел
# намеренно не в этом наборе: числа в прозе всегда окружены пробелами, и
# запрет на них исключил бы вообще все совпадения.
_NUMBER_ADJACENT = re.compile(r"[\d.,\xa0 ]")


def _is_standalone_number(text: str, start: int, end: int) -> bool:
    before_ok = start == 0 or not _NUMBER_ADJACENT.match(text[start - 1])
    after_ok = end == len(text) or not _NUMBER_ADJACENT.match(text[end])
    return before_ok and after_ok


def _classify_context(before: str, after: str) -> str | None:
    if _MULT_SUFFIX_CTX.match(after):
        return "ratio"
    if _PERCENT_SUFFIX_CTX.match(after):
        return "share"
    if _CURRENCY_PREFIX_CTX.search(before) or _CURRENCY_SUFFIX_CTX.match(after):
        return "absolute"
    return None


def _threshold_form(limit: str, quote: str) -> str | None:
    """Форма порога — по тому, чем число окружено в самой цитате пункта, а не
    по его величине. 'ratio'/'share' сравниваются с family_of как один класс
    «отношение» (см. _shape_conflicts) — раскол ratio/share у family_of
    обслуживает другую задачу (выброс по семье метрики).

    Несколько РАЗНЫХ форм у разных вхождений одного числа в цитате —
    неоднозначность, а не выбор: возвращаем None. Форма не найдена нигде —
    тоже None. Это главный отказ правила: оно вправе говорить только тогда,
    когда текст сказал явно."""
    degrouped = _degroup_thousands(quote)
    found: set[str] = set()
    for text in {quote, degrouped}:
        for form in _limit_forms(limit):
            if not form:
                continue
            for m in re.finditer(re.escape(form), text):
                if not _is_standalone_number(text, m.start(), m.end()):
                    continue
                before = text[max(0, m.start() - _SHAPE_CONTEXT_BEFORE) : m.start()]
                after = text[m.end() : m.end() + _SHAPE_CONTEXT_AFTER]
                cls = _classify_context(before, after)
                if cls:
                    found.add(cls)
    return found.pop() if len(found) == 1 else None


def _shape_bucket(form: str) -> str:
    return "absolute" if form == "absolute" else "ratio"


def _shape_conflicts(threshold_form: str | None, metric_family: str | None) -> bool:
    """Конфликт формы — порог денежный, а метрика отношение, или наоборот.
    Молчит, если хотя бы одна сторона неизвестна: это не «формы совпали», а
    «мнения не имеем» (см. докстринг _threshold_form)."""
    if threshold_form is None or metric_family is None:
        return False
    return _shape_bucket(threshold_form) != _shape_bucket(metric_family)


_CLAUSE_HEAD = r"(?:Пункт|Статья|Article|Section|Clause)"


def _clause_span(agreement_text: str, clause: str) -> str:
    """Текст пункта договора: от его заголовка до следующего заголовка.

    Граница по заголовку, а не по длине: определение порога («Разрешённая
    величина означает...») стоит в конце тела пункта, а произвольный потолок
    в символах был бы магическим числом. Не нашли заголовок (другая
    вёрстка) — пустая строка, вызывающий трактует как «не найдено»."""
    if not agreement_text or not clause:
        return ""
    m = re.search(_CLAUSE_HEAD + r"\s+" + re.escape(clause) + r"(?![\d.])", agreement_text)
    if not m:
        return ""
    nxt = re.search(_CLAUSE_HEAD + r"\s+\d", agreement_text[m.end() :])
    end = m.end() + nxt.start() if nxt else len(agreement_text)
    return agreement_text[m.start() : end]


def _limit_in_clause_span(limit: str, agreement_text: str, clause: str) -> bool:
    """Порог напечатан в тексте ТОГО ЖЕ пункта договора (вне цитаты).

    Формы — ровно те же, что у _limit_in_quote: проверка не либеральнее,
    просто корпус — тело пункта настоящего договора вместо цитаты."""
    span = _clause_span(agreement_text, clause)
    return bool(span) and _limit_in_quote(limit, span)


def _heading_key_from_text(agreement_text: str, clause: str) -> str:
    """Ключ заголовка пункта — из ТЕКСТА договора, не из цитаты модели.

    Модель цитирует тело обязательства («Заёмщик обязуется...»), а заголовок
    печатается в документе перед телом («Пункт N.M <Заголовок>. Заёмщик...»)
    и в цитату не попадает — замер по публичному набору: 0/36 совпадений
    title_key(quote) с TEMPLATE_HEADINGS против 36/36 у заголовка из текста.
    Ищем строку заголовка по номеру пункта; не нашли (другая вёрстка на
    приватном наборе) — вернём пустую строку, и вызывающий откатится на
    прежний ключ из цитаты (матч тогда закроет резервная сигнатура).
    """
    if not agreement_text or not clause:
        return ""
    pattern = re.compile(
        r"(?:Пункт|Статья|Article|Section|Clause)\s+" + re.escape(clause) + r"[\s.:—-]+([^.\n]{3,120})[.\n]"
    )
    m = pattern.search(agreement_text)
    return title_key(m.group(1)) if m else ""


def _check(sp: dict, fact_keys: set[str], agreement_text: str) -> tuple[dict, object | None]:
    out = {
        **sp,
        "valid": False,
        "errors": [],
        "template": None,
        "missing_doc_keys": [],
        "trigger_discarded": False,
        "title_key": _heading_key_from_text(agreement_text, sp.get("clause", "")) or title_key(sp["quote"]),
    }
    try:
        node = parse(sp["metric"])
    except DslError as exc:
        out["errors"].append(f"metric: {exc}")
        return out, None
    metric_missing = sorted({n.key for n in walk(node) if isinstance(n, Doc)} - fact_keys)
    out["missing_doc_keys"] = metric_missing
    errors = [e for e in validate(node, fact_keys) if "doc-ключ" not in e]

    # Порог — самая чувствительная точка prompt-injection: подменённый limit
    # тихо переворачивает вердикт, поэтому и цитата, и порог обязаны быть
    # верифицируемы в исходном тексте договора, а не просто правдоподобны.
    # И порог обязан быть ЧИСЛОМ уже здесь: «грамматика проверяет до
    # исполнения» — иначе «5%» доехал бы валидным до Decimal() в solve
    # и ушёл на лестницу без алярма invalid_spec (ревью PR #9, 6-я волна).
    try:
        limit_decimal = Decimal(sp["limit"])
    except (InvalidOperation, TypeError):
        limit_decimal = None
        errors.append("limit: не число")
    if not verify_quote(sp["quote"], agreement_text):
        errors.append("quote_unverified")
    elif not _limit_in_quote(sp["limit"], sp["quote"]):
        # Порог, определённый в том же пункте, но вне цитаты: «...не допускать
        # превышения Разрешённой величины», а «Разрешённая величина означает
        # 5 процентов...» стоит двумя предложениями ниже (боевой прогон
        # 2026-08-09). Цитата покрывает обязательство, определение — нет.
        # Защитная семантика сохранена: число обязано быть НАПЕЧАТАНО в тексте
        # того же пункта настоящего договора — подсунутый промптом порог в
        # тексте пункта не стоит. Совпадение помечается, а не молчит.
        if _limit_in_clause_span(sp["limit"], agreement_text, sp.get("clause", "")):
            out["limit_matched_in_clause"] = True
        else:
            errors.append("limit_not_in_quote")
    elif limit_decimal is not None and not isinstance(node, Doc):
        # Форма порога определяет форму метрики (правило команды со 2-го
        # места). Ветка достижима только когда цитата верифицирована И порог
        # напечатан прямо в ней — «порог не найден» и «метрика не
        # распарсилась» отсекаются раньше и не дублируются здесь.
        #
        # Голый doc(ключ) в качестве ВСЕЙ метрики исключён отдельно: family_of
        # видит только AST, а doc() — непрозрачное значение, посчитанное вне
        # DSL (боевой прогон приватного набора: кратностный порог квартальной
        # доли выручки от годовой модель выразила doc(...) вместо ratio(...),
        # family_of дал 'absolute', и верная ячейка легла на лестницу — false
        # positive, не тот случай, для которого заводилось правило).
        # ratio(doc(...), ...) под это исключение не попадает — там doc()
        # лишь один из аргументов, а верхний узел по-прежнему Ratio, и
        # family_of классифицирует его корректно.
        threshold_shape = _threshold_form(sp["limit"], sp["quote"])
        metric_shape = family_of(node, limit_decimal)
        if _shape_conflicts(threshold_shape, metric_shape):
            errors.append("limit_shape_mismatch")
            out["shape_mismatch"] = {"threshold_shape": threshold_shape, "metric_shape": metric_shape}

    trig_value = sp["trigger"]
    if trig_value:
        discard = False
        try:
            trig_node = parse(trig_value)
            trig_errors = [e for e in validate(trig_node, fact_keys) if "doc-ключ" not in e]
            discard = bool(trig_errors) or not isinstance(trig_node, Cmp)
            # doc()-ключи триггера (ревью PR #9, 8-я волна): без учёта они не
            # попадали ни в missing (resolve не получал шанса), ни в отброс —
            # спека была valid и падала KeyError в evaluate. Даём резолву шанс
            # через missing_doc_keys; ключ так и не нашёлся — мягкий отброс
            # триггера, не ячейки (та же логика, что у кривого триггера).
            if not discard:
                trig_missing = sorted({n.key for n in walk(trig_node) if isinstance(n, Doc)} - fact_keys)
                if trig_missing:
                    # В missing_doc_keys — чтобы resolve получил шанс (на
                    # ре-чеке после резолва ключ найдётся и триггер выживет);
                    # валидность спеки при этом меряется ТОЛЬКО по ключам
                    # метрики — нерешённый триггер-ключ стоит триггера, не ячейки.
                    out["missing_doc_keys"] = sorted(set(out["missing_doc_keys"]) | set(trig_missing))
                    discard = True
        except DslError:
            discard = True
        if discard:
            # Кривой/ложный триггер (например «период действия договора») не
            # должен стоить ячейку: метрика и порог валидны сами по себе.
            trig_value = None
            out["trigger_discarded"] = True
    out["trigger"] = trig_value

    out["errors"] = errors
    out["valid"] = not errors and not metric_missing
    out["template"] = match_signature(node) if out["valid"] else None
    return out, node


def _median(values: list[Decimal]) -> Decimal:
    vs = sorted(values)
    n = len(vs)
    mid = n // 2
    return vs[mid] if n % 2 else (vs[mid - 1] + vs[mid]) / 2


def _flag_outliers(clauses: dict, parsed_nodes: dict[str, object], alarms: list[dict]) -> None:
    """Порог, отличающийся на порядок и более от медианы своей семьи метрики
    в этом прогоне, не роняет ячейку — только помечает её на глаза.

    parsed_nodes — узлы, уже распарсенные в _check; повторный parse() тут не нужен."""
    parsed: dict[str, tuple[object, Decimal]] = {}
    for key in sorted(parsed_nodes):
        try:
            limit = Decimal(clauses[key]["limit"])
        except InvalidOperation:
            continue
        parsed[key] = (parsed_nodes[key], limit)
    by_family: dict[str, list[Decimal]] = {}
    for node, limit in parsed.values():
        fam = family_of(node, limit)
        if fam is not None:
            by_family.setdefault(fam, []).append(limit)
    for key in sorted(parsed):
        node, limit = parsed[key]
        fam = family_of(node, limit)
        peers = by_family.get(fam, []) if fam is not None else []
        if len(peers) < 2 or limit == 0:
            continue
        med = _median(peers)
        if med == 0:
            continue
        ratio = limit / med if limit > med else med / limit
        if ratio >= _OUTLIER_FACTOR:
            alarms.append({"kind": "limit_outlier", "clause": key})


def extract_specs(wd: Path, dossier_art: dict, fact_keys: set[str]) -> dict:
    acc = dossier_art["account_id"]
    agreements = [d for d in dossier_art["docs"] if d["doc_type"] == "agreement"]

    def build() -> dict:
        if not agreements:
            return {"covenants": [], "alarms": [{"kind": "no_agreement", "account": acc}]}
        text = sanitize_document(agreements[0]["text"])
        prompt = (
            DATA_NOT_COMMANDS
            + "\n\n"
            + SPECS_PROMPT.format(
                categories=", ".join(sorted(LEAVES)),
                fact_keys=", ".join(sorted(fact_keys)) or "(пока нет)",
                text=text,
            )
        )
        try:
            raw = llm.call(prompt, SPECS_SCHEMA, SCHEMA_VERSION, max_tokens=16000)
        except llm.SchemaRejected as exc:
            # account внутрь build() (как у no_agreement, ревью PR #9, 19-я
            # волна): иначе сырая копия на диске и обогащённая при чтении —
            # разные словари, глобальный дедуп их не схлопывает и счётчик в
            # run-report удваивается.
            return {
                "covenants": [],
                "alarms": [{"kind": "specs_extraction_failed", "account": acc, "error": str(exc)}],
            }
        return {"covenants": raw["covenants"], "alarms": []}

    # Провал извлечения (и пустое досье без договора) не кэшируется: артефакт
    # инвалидируется только по версии, и запечённая деградация пережила бы
    # перезапуск в окне после устранения причины (ревью PR #9, 22-я волна —
    # тот же механизм залипания, что degraded в dossier). Пересбор no_agreement
    # бесплатен: build() возвращается до LLM-вызова.
    _degraded_kinds = {"specs_extraction_failed", "no_agreement"}
    raw_art = artifact(
        wd / "specs" / f"{acc}.json",
        SPECS_STAGE_VERSION,
        build,
        cache_if=lambda d: not any(a.get("kind") in _degraded_kinds for a in d["alarms"]),
    )
    agreement_text = sanitize_document(agreements[0]["text"]) if agreements else ""

    alarms = list(raw_art["alarms"])
    clauses: dict[str, dict] = {}
    parsed_nodes: dict[str, object] = {}
    for sp in raw_art["covenants"]:
        clause_key, found = _normalize_clause(sp["clause"])
        if not found:
            alarms.append({"kind": "clause_unmatched", "clause": sp["clause"]})
        if clause_key in clauses:
            alarms.append({"kind": "duplicate_clause", "clause": clause_key})
            continue
        # Нормализация — при чтении, как и вся валидация: сырой артефакт не
        # меняется, уже закэшированные ответы с валютной вёрсткой порога
        # самоизлечиваются без пересбора и без LLM.
        checked, node = _check(
            {**sp, "clause": clause_key, "limit": _normalize_limit(sp["limit"])}, fact_keys, agreement_text
        )
        if checked.pop("trigger_discarded", False):
            alarms.append({"kind": "trigger_discarded", "clause": clause_key})
        if checked.pop("limit_matched_in_clause", False):
            # Видимость нестандартного пути: порог сматчился не в цитате, а в
            # теле пункта — на приватном наборе это надо видеть поимённо.
            alarms.append({"kind": "limit_matched_in_clause_text", "clause": clause_key})
        shape_mismatch = checked.pop("shape_mismatch", None)
        if shape_mismatch is not None:
            # Какая форма ожидалась (из текста порога) и какая пришла (из
            # формулы модели) — поимённо, а не только errors: "limit_shape_mismatch".
            alarms.append({"kind": "limit_shape_mismatch", "clause": clause_key, **shape_mismatch})
        clauses[clause_key] = checked
        if node is not None:
            parsed_nodes[clause_key] = node
        if not checked["valid"] and not checked["missing_doc_keys"]:
            alarms.append({"kind": "invalid_spec", "clause": clause_key, "errors": checked["errors"]})
        elif checked["missing_doc_keys"]:
            # Тихих отбросов нет (ревью PR #9, 8-я волна): недостающие doc-ключи
            # видны сразу, а не реверс-инжинирятся по tier в трейсе.
            alarms.append(
                {"kind": "missing_doc_keys", "clause": clause_key, "keys": checked["missing_doc_keys"]}
            )

    _flag_outliers(clauses, parsed_nodes, alarms)
    # account в каждом алярме (ревью PR #9, 15-я волна): без него одинаковая
    # системная поломка у разных заёмщиков схлопывалась бы глобальным дедупом
    # точных дублей в run-report до «1». Обогащение — при ЧТЕНИИ, артефакт
    # (сырой ответ модели) не меняется, версия стадии не бампается.
    return {"clauses": clauses, "alarms": [{**a, "account": acc} for a in alarms]}
