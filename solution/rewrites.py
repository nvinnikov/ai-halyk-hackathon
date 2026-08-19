"""Переписывание финального AST ячейки перед расчётом.

Здесь живут правки, которые нельзя доверить модели, потому что цена ошибки
несимметрична, а признак — механический. Модуль сознательно не трогает
промпты: ключ LLM-кэша считается от текста промпта, и правка промпта стоила бы
всей кассеты, то есть возможности мерить изменение офлайн.
"""

import dataclasses
import re
from decimal import Decimal, InvalidOperation

from dsl import (
    Add,
    Agg,
    Const,
    CounterpartyIn,
    Doc,
    MaxOf,
    MinOf,
    Mul,
    Period,
    Quarter,
    Ratio,
    Sub,
    unparse,
    walk,
)
from specs_extract import _limit_in_quote
from taxonomy import LEAVES

# Маркеры прямого широкого прочтения: договор сам говорит «все операционные».
# Это ЕДИНСТВЕННЫЙ путь к роллапу OPEX_TOTAL — подсчёт маркеров-статей
# (аренда, коммуналка, страхование и т.п.) по всей цитате пункта был отвергнут
# раундом правок 1: одна из ячеек приватного набора (коэффициент покрытия
# постоянных платежей) перечисляет ИМЕННО эти статьи как
# ЗНАМЕНАТЕЛЬ ковенанта (сумму фиксированных платежей), а не как состав
# вычитаемого в EBITDA, и подсчёт слов не может отличить одно от другого —
# у подсчёта нет доступа к тому, к какой части формулы относится
# перечисление. Единственный надёжный сигнал — прямое высказывание «все
# операционные расходы» именно о EBITDA.
_TOTAL_MARKERS = (
    "всех операционных",
    "все операционные",
    "всеми операционными",
    "совокупных операционных",
    "совокупные операционные",
    "total operating expense",
    "all operating expense",
)


def _quote_reads_broadly(quote: str) -> bool:
    t = (quote or "").lower()
    return any(m in t for m in _TOTAL_MARKERS)


def narrow_opex(metric_ast, quote: str) -> tuple[object, bool]:
    """EBITDA считается по статье, если договор не сказал обратного явно.

    Роллап OPEX_TOTAL остаётся законным вторым прочтением, но перестаёт быть
    прочтением ПО УМОЛЧАНИЮ: на приватном наборе он не был верен ни разу, а
    цена ошибки в его сторону — двукратный порядок в знаменателе EBITDA
    (355 млн против 3.2 млн у одного заёмщика). Переписывается только
    EBITDA-подвыражение sub(выручка, опекс) — ровно та же граница, что у
    solve._apply_ebitda_reading: ковенант о доле консультационных в
    операционных расходах оперирует своим роллапом независимо.

    Вычитаемое узнаётся в двух формах (раунд правок 1, дефект №2, ячейка
    приватного набора про минимальный запас покрытия постоянных расходов):
    голый agg(OPEX_TOTAL, ...) и add(...), среди прямых
    аргументов которого есть agg(OPEX_TOTAL, ...) — договор вычитает
    несколько названных статей суммой, и роллап внутри этой суммы столь же
    ошибочен, как единственное вычитаемое. Остальные аргументы add (ФОТ,
    аренда как отдельные названные статьи) не трогаются — граница уже, чем
    «весь add», ровно на ту одну категорию, которая и есть неправильный
    дефолт."""
    if _quote_reads_broadly(quote):
        return metric_ast, False

    changed = False

    def rewrite(node):
        nonlocal changed
        if isinstance(node, Sub) and isinstance(node.a, Agg) and node.a.category == "REVENUE":
            b = node.b
            if isinstance(b, Agg) and b.category == "OPEX_TOTAL":
                changed = True
                return Sub(a=node.a, b=dataclasses.replace(b, category="OTHER_OPEX"))
            if isinstance(b, Add) and any(
                isinstance(arg, Agg) and arg.category == "OPEX_TOTAL" for arg in b.args
            ):
                changed = True
                new_args = tuple(
                    dataclasses.replace(arg, category="OTHER_OPEX")
                    if isinstance(arg, Agg) and arg.category == "OPEX_TOTAL"
                    else arg
                    for arg in b.args
                )
                return Sub(a=node.a, b=dataclasses.replace(b, args=new_args))
        if not hasattr(node, "__dataclass_fields__"):
            return node
        # Несовпавший узел (в т.ч. Sub другой формы) всё равно спускается в
        # детей — вложенная форма sub(sub(...), ...) иначе не переписалась бы.
        updates = {}
        for name in node.__dataclass_fields__:
            value = getattr(node, name)
            if isinstance(value, tuple):
                updates[name] = tuple(rewrite(c) if hasattr(c, "__dataclass_fields__") else c for c in value)
            elif hasattr(value, "__dataclass_fields__"):
                updates[name] = rewrite(value)
        return dataclasses.replace(node, **updates) if updates else node

    out = rewrite(metric_ast)
    return (out, changed) if changed else (metric_ast, False)


# Категории, которые в этом конвейере вообще бывают вычитаемым в
# EBITDA-подвыражении (narrow_opex/solve._apply_ebitda_reading различают
# ровно эти две — статья и роллап). Addback-переписывание опирается на тот
# же канон, а не на «любой agg», чтобы не расширять границу за пределы того,
# что остальной EBITDA-механизм вообще умеет строить.
_EBITDA_OPEX_CATEGORIES = ("OTHER_OPEX", "OPEX_TOTAL")
_EBITDA_ADDBACK_DOC_KEY = "ebitda_addbacks_material_total"


def _is_ebitda_opex_subtrahend(node: object) -> bool:
    if isinstance(node, Agg):
        return node.category in _EBITDA_OPEX_CATEGORIES
    if isinstance(node, Add):
        return any(isinstance(arg, Agg) and arg.category in _EBITDA_OPEX_CATEGORIES for arg in node.args)
    return False


def add_ebitda_addback(metric_ast, needs_addback: bool) -> tuple[object, bool]:
    """Разовая корректировка EBITDA, разрешённая договором (задача 3).

    Определение EBITDA вправе одновременно сузить статью опекса (narrow_opex)
    И потребовать учесть разовые статьи, добавленные обратно по согласованию
    аудитора, — это ОТДЕЛЬНЫЙ признак договора, не третье прочтение. Поэтому
    вход — уже готовый булев вердикт (facts_extract._quote_requires_addback),
    а не текст цитаты: само переписывание ничего не парсит.

    Переписывается ТОЛЬКО EBITDA-подвыражение — sub(agg(REVENUE, in, ...),
    agg(<категория опекса>, out, ...)), включая форму, где вычитаемое
    составное (add с категорией опекса среди прямых аргументов) — ровно та
    же граница, что у narrow_opex. Всякий иной агрегат (числитель левереджа,
    платежи связанным сторонам и т.п.) не трогается: их a — не REVENUE, и
    условие входа их не пропускает.

    Идемпотентно: если derived-ключ корректировки уже встречается где-то в
    дереве — переписывание молчит целиком. Часть заёмщиков вписывает addback
    в формулу сама (модель прочла его прямо в тексте самого пункта, а не в
    отдельном определении EBITDA, — наблюдалось на приватном наборе), и
    повторное добавление удвоило бы корректировку."""
    if not needs_addback or metric_ast is None:
        return metric_ast, False
    if any(isinstance(n, Doc) and n.key == _EBITDA_ADDBACK_DOC_KEY for n in walk(metric_ast)):
        return metric_ast, False

    changed = False

    def rewrite(node):
        nonlocal changed
        if (
            isinstance(node, Sub)
            and isinstance(node.a, Agg)
            and node.a.category == "REVENUE"
            and _is_ebitda_opex_subtrahend(node.b)
        ):
            changed = True
            return Add(args=(node, Doc(key=_EBITDA_ADDBACK_DOC_KEY)))
        if not hasattr(node, "__dataclass_fields__"):
            return node
        updates = {}
        for name in node.__dataclass_fields__:
            value = getattr(node, name)
            if isinstance(value, tuple):
                updates[name] = tuple(rewrite(c) if hasattr(c, "__dataclass_fields__") else c for c in value)
            elif hasattr(value, "__dataclass_fields__"):
                updates[name] = rewrite(value)
        return dataclasses.replace(node, **updates) if updates else node

    out = rewrite(metric_ast)
    return (out, changed) if changed else (metric_ast, False)


def _is_related_party_filter(f: object) -> bool:
    return isinstance(f, CounterpartyIn) and f.setname == "related_parties"


def widen_related_party(metric_ast, quote: str) -> tuple[object, bool]:
    """Ковенант об оттоке к связанным сторонам не привязан к статье учёта.

    «Ограниченные платежи / выплаты / распределения в пользу связанных
    сторон» ограничивают ЛЮБОЙ отток к связанной стороне, а модель выбирает
    статью по описанию платежа и промахивается: платёж, проведённый как
    консультационный или прочий операционный, попадает в узкую категорию
    (`FINANCING`, `OTHER`), под которой в леджере связанной стороны нет ни
    строки. Признак ковенанта надёжен и не завязан на текст цитаты — это
    фильтр `counterparty_in(related_parties)` на самом агрегате: если он
    есть, категория заменяется на `ALL`, знак и остальные фильтры сохраняются.

    Лист таксономии — обязательное условие: агрегат уже с ролловер-категорией
    (`ALL`, `OPEX_TOTAL`) не трогаем — расширять нечего, а `OPEX_TOTAL` под
    фильтром связанных сторон в природе не встречается и переписывать его как
    лист было бы гаданием.

    Знаменатель отношения не трогаем НИКОГДА, даже когда там тот же фильтр:
    `related_share_revenue`/`related_share_opex` меряют ДОЛЮ платежей
    связанным сторонам в выручке/операционных расходах, и категория
    знаменателя там несёт смысл ковенанта, а не промах модели — расширение
    до `ALL` завысило бы знаменатель и исказило верный ответ.

    `quote` в сигнатуре не используется: признак здесь структурный (наличие
    фильтра на самом узле), а не текстовый — сигнатура одного вида с
    `narrow_opex`/`quarterly` для единообразного вызова из `apply_final`."""
    changed = False

    def rewrite(node, in_denominator: bool):
        nonlocal changed
        if isinstance(node, Ratio):
            return dataclasses.replace(
                node,
                num=rewrite(node.num, in_denominator),
                den=rewrite(node.den, True),
            )
        if (
            not in_denominator
            and isinstance(node, Agg)
            and node.category in LEAVES
            and any(_is_related_party_filter(f) for f in node.filters)
        ):
            changed = True
            return dataclasses.replace(node, category="ALL")
        if not hasattr(node, "__dataclass_fields__"):
            return node
        updates = {}
        for name in node.__dataclass_fields__:
            value = getattr(node, name)
            if isinstance(value, tuple):
                updates[name] = tuple(
                    rewrite(c, in_denominator) if hasattr(c, "__dataclass_fields__") else c for c in value
                )
            elif hasattr(value, "__dataclass_fields__"):
                updates[name] = rewrite(value, in_denominator)
        return dataclasses.replace(node, **updates) if updates else node

    out = rewrite(metric_ast, False)
    return (out, changed) if changed else (metric_ast, False)


_WORD_RE = re.compile(r"[\w-]+", re.UNICODE)
_SENTENCE_SPLIT_RE = re.compile(r"[.;]+")


# Основы слов о ПРИВЛЕЧЕНИИ задолженности против основ слов о ЕЁ ПОГАШЕНИИ
# (обслуживании). Основы, а не целые формы — падеж и род русских договоров
# гуляют, а корень один: «привлечённая», «привлечение», «привлекла» —
# одна основа. Английские формы нужны отдельно: часть договоров набора на
# английском.
#
# Ярус привлечения расколот надвое (раунд правок 1). НАДЁЖНЫЕ основы сами по
# себе однозначно про долг («привлечение», «заимствование», incurred,
# borrowing) — соседство им не требуется. РИСКОВАННЫЕ («наращива»/«нараста» —
# обычные слова о росте ЛЮБОЙ величины: резервов, капитала, доли; «выборк» —
# ещё и лексика аудиторских заключений, где это sample, а не выборка транша)
# сами по себе ничего не говорят про долг: без проверки соседства они
# переворачивали бы знак АКТИВНО НЕВЕРНО на цитате вроде «наращивание
# резервов на возможные потери». Признак для них — слово о
# задолженности/долге в ограниченном окне, не пересекающем границу
# предложения: тот же приём, что у `_sentence_mentions_any_quarter`
# (квантор и «квартал» рядом, окно ограничено, граница предложения его
# обнуляет) — переиспользуется, а не изобретается заново.
_INCURRENCE_STEMS_RELIABLE = (
    "привлечен",
    "привлека",
    "заимствован",
    "incurrence",
    "incurred",
    "incurring",
    "drawdown",
    "borrowing",
)
_INCURRENCE_PHRASE_STEMS = ("draw down",)
_INCURRENCE_STEMS_RISKY = ("наращива", "нараста", "выборк")
_DEBT_WORD_STEMS = ("задолженност", "долг", "заем", "займ", "кредит", "indebtedness", "debt", "loan")
_DEBT_WORD_GAP = 4

_REPAYMENT_WORD_STEMS = ("погашен", "погаша", "обслуживан", "возврат", "repayment", "repaid", "repay")
_REPAYMENT_PHRASE_STEMS = ("debt service", "amortization of principal")


def _normalize(quote: str) -> str:
    # «ё» приводим к «е»: в договорах написание пляшет («привлечённой» и
    # «привлеченной» — одно слово), а основа без буквы «ё» одна.
    return (quote or "").lower().replace("ё", "е")


def _word_matches_stem_unnegated(word: str, stems: tuple[str, ...]) -> bool:
    """Слово несёт основу, но не как отрицание, слитое с ней («не» + основа
    одним словом). «Непогашенная задолженность» — стандартный банковский
    термин про остаток, который ЕЩЁ предстоит погасить, то есть говорит
    ровно ПРОТИВОПОЛОЖНОЕ погашению; без этой защиты подстрока «погашен»
    внутри «непогашенная» ошибочно блокировала бы починку знака отказом
    (раунд правок 1, находка 1) — направление безопасное (недочинка, не
    порча), но частое достаточно, чтобы стоило чинить."""
    if not any(word.startswith(s) for s in stems):
        return False
    return not any(word.startswith("не" + s) for s in stems)


def _sentence_mentions_risky_incurrence(sentence: str) -> bool:
    words = _WORD_RE.findall(sentence)
    for i, word in enumerate(words):
        if not any(word.startswith(s) for s in _INCURRENCE_STEMS_RISKY):
            continue
        lo = max(0, i - _DEBT_WORD_GAP)
        hi = i + _DEBT_WORD_GAP + 1
        window = words[lo:i] + words[i + 1 : hi]
        if any(any(w.startswith(d) for d in _DEBT_WORD_STEMS) for w in window):
            return True
    return False


def _mentions_incurrence(quote: str) -> bool:
    t = _normalize(quote)
    if any(p in t for p in _INCURRENCE_PHRASE_STEMS):
        return True
    words = _WORD_RE.findall(t)
    if any(_word_matches_stem_unnegated(w, _INCURRENCE_STEMS_RELIABLE) for w in words):
        return True
    return any(_sentence_mentions_risky_incurrence(s) for s in _SENTENCE_SPLIT_RE.split(t))


def _mentions_repayment(quote: str) -> bool:
    t = _normalize(quote)
    if any(p in t for p in _REPAYMENT_PHRASE_STEMS):
        return True
    words = _WORD_RE.findall(t)
    return any(_word_matches_stem_unnegated(w, _REPAYMENT_WORD_STEMS) for w in words)


def flip_debt_incurrence_sign(metric_ast, quote: str) -> tuple[object, bool]:
    """«Привлечённая за период» задолженность — это приток, а не отток.

    Модель выбирает знак по интуиции о категории FINANCING («это расход по
    финансовой деятельности») и промахивается: погашение долга — отток,
    привлечение нового долга (транша, займа) — приток. Договор про
    «совокупную основную сумму Финансовой задолженности, привлечённой за
    период» описывает именно второе, а извлечённая формула считает
    agg(FINANCING, out) — то есть противоположную величину, и на леджере, где
    отток по этой категории за период нулевой, агрегат тихо даёт ноль.

    Признак — по цитате пункта, а не по имени категории: наличие основ слов о
    привлечении (см. `_mentions_incurrence` про два яруса надёжности) при
    отсутствии слов о погашении/обслуживании. Если в цитате есть и то и
    другое — переписывание молчит целиком: коэффициент покрытия обслуживания
    долга (DSCR) читает обе стороны в одной формуле, и слепая правка знака
    сломала бы верный ответ так же, как неверный знак сейчас ломает эту
    ячейку. Различить, какая половина цитаты относится к узлу с знаком out,
    здесь нечем — отказ дешевле угадывания.

    Категория ограничена FINANCING сознательно: это единственная категория,
    под которой в леджере встречаются движения по привлечению/погашению
    долга, и расширять признак на другие категории — гадание без опоры на
    наблюдаемый случай."""
    if not _mentions_incurrence(quote) or _mentions_repayment(quote):
        return metric_ast, False

    changed = False

    def rewrite(node):
        nonlocal changed
        if isinstance(node, Agg) and node.category == "FINANCING" and node.sign == "out":
            changed = True
            return dataclasses.replace(node, sign="in")
        if not hasattr(node, "__dataclass_fields__"):
            return node
        updates = {}
        for name in node.__dataclass_fields__:
            value = getattr(node, name)
            if isinstance(value, tuple):
                updates[name] = tuple(rewrite(c) if hasattr(c, "__dataclass_fields__") else c for c in value)
            elif hasattr(value, "__dataclass_fields__"):
                updates[name] = rewrite(value)
        return dataclasses.replace(node, **updates) if updates else node

    out = rewrite(metric_ast)
    return (out, changed) if changed else (metric_ast, False)


# --- задача 4: капнутая разрешённая корзина связанных сторон -----------------
#
# Ковенант вида «платежи связанным сторонам, за вычетом разрешённой корзины в
# размере ДО $X таких платежей, надлежащим образом квалифицированных как
# <статья>» резолвится сегодня буквальным потолком — doc(<ключ>) равен $X
# целиком, и метрика вычитает X, а не фактическую сумму квалифицированных
# платежей. Корзина — минимум из двух величин: потолка и факта. Верная форма:
# min(agg(<статья>, out, <фильтры агрегата платежей связанным сторонам>),
# const(X)).
#
# Признак — по ЦИТАТЕ КОНКРЕТНОГО doc-ключа (doc_fact_quotes[key]), а не по
# цитате всего пункта: doc-ключ резолвится адресно, и его собственная цитата —
# это ровно то предложение договора, где корзина описана. Три условия, все
# обязательны (однозначность несущая — молчаливый выбор дороже отказа):
#   1) оборот про исключение/вычет корзины («корзин»/«basket»);
#   2) потолок в виде «до $X» / «up to $X», и это ЖЕ значение (не любое число)
#      лежит в doc_facts[key] — проверяется _limit_in_quote, той же функцией,
#      которой конвейер уже сверяет пороги спек с их цитатами (эхо-гард,
#      solution/specs_extract.py). Один вызов закрывает оба заявленных отказа
#      («потолка нет» и «значение doc-ключа не совпадает с потолком») — это
#      один и тот же факт: число обязано быть буквально в тексте цитаты;
#   3) статья, которой платежи «надлежащим образом квалифицированы»,
#      сопоставляется РОВНО ОДНОЙ категории таксономии.

_BASKET_MARKERS = ("корзин", "basket")

# «до $X» / «up to $X» — потолок корзины, а не голое число где-то в
# предложении. Слово целиком (\b), не основа: «до» короче любого осмысленного
# стема и без границы слова ловило бы что угодно.
_UP_TO_RE = re.compile(r"\bдо\b\s*\$?\s*\d|\bup\s+to\b\s*\$?\s*\d|\bне\s+более\b\s*\$?\s*\d", re.IGNORECASE)

# Фраза, называющая статью: «квалифицированных как <статья>» / «qualified as
# <статья>». Захватывает всё до ближайшего знака препинания — этого достаточно
# для короткого названия статьи в разрешённой корзине (см. живые цитаты
# приватного набора в docstring cap_related_party_basket).
_STATUTE_PHRASE_RE = re.compile(
    r"(?:квалифицирован\w*\s+как|qualif(?:y|ied)\s+as|classified\s+as|characteri[sz]ed\s+as)\s+"
    r"([^.,;)\n]+)",
    re.IGNORECASE,
)

# Статья названа в тексте ДОГОВОРА (оба языка), не в описании проводки
# леджера — это другой словарь природы, чем categorize.RULES: тот распознаёт
# статью по ОПИСАНИЮ конкретной операции («advisory engagement», «retainer
# fee», всегда по-английски), а здесь — формальное НАЗВАНИЕ статьи в пункте
# договора («Консультационные услуги» / «Consulting Services»), которого в
# RULES нет ни разу. Домен категорий — LEAVES (см. проверку ниже), стем-приём —
# тот же, что у _mentions_incurrence/_mentions_repayment выше: основы слов,
# проверяемые по токену (word.startswith), а не подстрокой по всему тексту —
# иначе "current" ложно матчился бы стемом "rent" внутри "curRENT".
#
# REVENUE добавлена ради процентных кэпов из doc-ключа («N% of Revenue») —
# resolve_percent_of_statute переиспользует ровно этот словарь, второй такой
# не заводится.
_STATUTE_CATEGORY_STEMS: dict[str, tuple[str, ...]] = {
    "CONSULTING": ("консультацион", "consulting"),
    "MARKETING": ("маркетинг", "реклам", "marketing", "advertis"),
    "INSURANCE": ("страхов", "insurance"),
    "PAYROLL": ("оплат труд", "заработн", "payroll"),
    "RENT": ("аренд", "rent", "lease"),
    "UTILITIES": ("коммунальн", "utilit"),
    "TELECOM": ("связ", "telecom"),
    "TAX": ("налог", "tax"),
    "INTEREST": ("процент", "interest"),
    "CAPEX": ("капитальн", "capex", "capital expenditure"),
    "OTHER_OPEX": ("операционн", "operating expense"),
    "REVENUE": ("выручк", "revenue"),
}
assert set(_STATUTE_CATEGORY_STEMS) <= LEAVES  # ключи — не выдуманные категории


def _mentions_basket(quote: str) -> bool:
    t = _normalize(quote)
    return any(m in t for m in _BASKET_MARKERS)


def _mentions_up_to_cap(quote: str) -> bool:
    return bool(_UP_TO_RE.search(_normalize(quote)))


def _match_category_by_phrase(phrase: str) -> str | None:
    """Единственная категория LEAVES, названная нормализованной фразой, или
    None — не распознано, либо распознано несколько (однозначность несущая:
    молчаливый выбор дороже отказа). Общая часть _match_statute_category и
    resolve_percent_of_statute — статья ищется одним и тем же способом,
    независимо от того, откуда взята сама фраза (цитата пункта или значение
    doc-ключа)."""
    phrase = _normalize(phrase)
    words = _WORD_RE.findall(phrase)
    matched = {
        category
        for category, stems in _STATUTE_CATEGORY_STEMS.items()
        for stem in stems
        if (stem in phrase if " " in stem else any(w.startswith(stem) for w in words))
    }
    return matched.pop() if len(matched) == 1 else None


def _match_statute_category(quote: str) -> str | None:
    """Единственная категория, названная статьёй в «квалифицированных как …»,
    или None — фразы нет, категория не распознана, либо распознано несколько."""
    m = _STATUTE_PHRASE_RE.search(quote)
    if m is None:
        return None
    return _match_category_by_phrase(m.group(1))


def _find_related_party_agg(node: object) -> Agg | None:
    """Первый Agg с фильтром связанных сторон в поддереве — образец фильтров
    и знака для нового агрегата корзины (задача 4: одни и те же period и
    counterparty_in, что у самого агрегата платежей связанным сторонам)."""
    for n in walk(node):
        if isinstance(n, Agg) and any(_is_related_party_filter(f) for f in n.filters):
            return n
    return None


def _capped_basket_node(numerator: object, key: str, doc_facts: dict, doc_fact_quotes: dict) -> object | None:
    quote = doc_fact_quotes.get(key)
    value = doc_facts.get(key)
    if not quote or value is None:
        return None
    if not _mentions_basket(quote) or not _mentions_up_to_cap(quote):
        return None
    # Один и тот же вызов закрывает оба отказа спеки задачи: «потолка нет в
    # цитате числом» и «значение doc-ключа не совпадает с потолком» — оба
    # означают одно и то же несовпадение буквальной формы value с текстом.
    if not _limit_in_quote(str(value), quote):
        return None
    category = _match_statute_category(quote)
    if category is None:
        return None
    related_agg = _find_related_party_agg(numerator)
    if related_agg is None:
        return None
    try:
        limit_value = Decimal(str(value))
    except InvalidOperation:
        return None
    basket_agg = Agg(category=category, sign=related_agg.sign, filters=related_agg.filters)
    return MinOf(args=(basket_agg, Const(value=limit_value)))


def cap_related_party_basket(
    metric_ast, doc_facts: dict | None, doc_fact_quotes: dict | None
) -> tuple[object, bool]:
    """«Разрешённая корзина в размере до $X» — минимум из потолка и факта.

    Ковенант платежей связанным сторонам вычитает не сам потолок, а меньшее
    из потолка и суммы платежей, ФАКТИЧЕСКИ надлежащим образом
    квалифицированных договором как названная статья: «до $X таких платежей,
    ... квалифицированных как <статья>» — это ПОТОЛОК корзины, а не
    гарантированный вычет. Сегодня doc(<ключ>) резолвится числом потолка
    напрямую (resolve_doc_fact берёт число из той же цитаты), и метрика
    вычитает его целиком — переплачивая корзиной там, где фактические
    квалифицированные платежи меньше потолка (например, часть переклассифицирована
    аудитором в другую статью и больше не «надлежащим образом квалифицирована»).

    Переписывается ТОЛЬКО Doc, стоящий вычитаемым (`b`) в Sub(a, Doc(key)),
    где `a` содержит агрегат с фильтром связанных сторон — оттуда берутся его
    period/counterparty_in и знак для нового агрегата корзины. Признание
    цитаты и категории — в `_capped_basket_node`; любой из трёх гейтов
    (корзина, потолок-в-цитате, однозначная статья) молчит целиком.

    doc_facts/doc_fact_quotes — уже готовые факты досье (doc_fact_quotes[key]
    — цитата, под которую резолвился именно ЭТОТ doc-ключ), не текст пункта
    целиком: адресный резолв даёт цитату уже, чем цитата всего ковенанта, и
    статья корзины называется именно в ней."""
    if metric_ast is None or not doc_facts or not doc_fact_quotes:
        return metric_ast, False

    changed = False

    def rewrite(node):
        nonlocal changed
        if isinstance(node, Sub) and isinstance(node.b, Doc):
            replacement = _capped_basket_node(node.a, node.b.key, doc_facts, doc_fact_quotes)
            if replacement is not None:
                changed = True
                return Sub(a=node.a, b=replacement)
        if not hasattr(node, "__dataclass_fields__"):
            return node
        updates = {}
        for name in node.__dataclass_fields__:
            value = getattr(node, name)
            if isinstance(value, tuple):
                updates[name] = tuple(rewrite(c) if hasattr(c, "__dataclass_fields__") else c for c in value)
            elif hasattr(value, "__dataclass_fields__"):
                updates[name] = rewrite(value)
        return dataclasses.replace(node, **updates) if updates else node

    out = rewrite(metric_ast)
    return (out, changed) if changed else (metric_ast, False)


# Ковенант вида «разовые добавления к EBITDA не более N% of Revenue» (или
# любой другой статьи, уже посчитанной где-то в этой же метрике) резолвится
# адресным числовым резолвом (facts_extract.resolve_doc_fact) в строку — «5%
# of Revenue», а не в число, потому что величина не названа суммой ни в одном
# документе. facts_extract больше не отбрасывает такую строку (_number_ok
# признаёт её опознаваемым процентным кэпом наравне с числом, тем же
# parse_percent_of_statute), но АРИФМЕТИКУ строка не несёт — её обязан
# посчитать код. Верная форма: mul(agg(<категория статьи>, <тот же знак/
# фильтры, что у одноимённого агрегата в метрике>), const(N/100)).
#
# «Одноимённый агрегат» — единственное, что даёт знак и фильтры (период,
# контрагентов): выдумывать их запрещено (см. брифинг задачи), поэтому
# подстановка требует РОВНО ОДНОГО Agg той же категории в метрике; ноль или
# больше одного — отказ, не гадание.
_PERCENT_OF_STATUTE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*%\s*(?:of|от)\s+([^.,;)\n]+)",
    re.IGNORECASE,
)


def parse_percent_of_statute(value: str) -> tuple[Decimal, str] | None:
    """«N% of/от <статья>» → (доля от 0 до 1, категория LEAVES), иначе None.

    Три гейта, все обязательны — молчаливая подмена смысла ковенанта дороже
    отказа:
      1) доля разбирается регуляркой из значения doc-ключа;
      2) доля в диапазоне (0; 100) — ноль и отрицательное не кэп, сто и
         больше не отсечение, а признак неверного разбора;
      3) статья сопоставляется РОВНО ОДНОЙ категории таксономии тем же
         словарём и матчером, что и корзина связанных сторон
         (_STATUTE_CATEGORY_STEMS, _match_category_by_phrase) — второй
         словарь не заводится.

    Переиспользуется facts_extract.resolve_doc_fact (гейт приёма значения) и
    resolve_percent_of_statute (сама подстановка) — один разбор на обоих."""
    if not value:
        return None
    m = _PERCENT_OF_STATUTE_RE.search(value)
    if m is None:
        return None
    try:
        pct = Decimal(m.group(1).replace(",", "."))
    except InvalidOperation:
        return None
    if not (Decimal(0) < pct < Decimal(100)):
        return None
    category = _match_category_by_phrase(m.group(2))
    if category is None:
        return None
    return pct / Decimal(100), category


def _find_agg_by_category(node: object, category: str) -> Agg | None:
    """Единственный Agg данной категории в дереве, иначе None — знак и
    фильтры нового агрегата берутся у него, а не выдумываются заново."""
    matches = [n for n in walk(node) if isinstance(n, Agg) and n.category == category]
    return matches[0] if len(matches) == 1 else None


def _percent_cap_node(key: str, doc_facts: dict, root: object) -> object | None:
    value = doc_facts.get(key)
    if value is None:
        return None
    parsed = parse_percent_of_statute(str(value))
    if parsed is None:
        return None
    fraction, category = parsed
    source_agg = _find_agg_by_category(root, category)
    if source_agg is None:
        return None
    return Mul(
        a=Agg(category=category, sign=source_agg.sign, filters=source_agg.filters), b=Const(value=fraction)
    )


def resolve_percent_of_statute(metric_ast, doc_facts: dict | None) -> tuple[object, bool]:
    """doc(<ключ>) → mul(agg(<категория>, <знак>, <фильтры>), const(доля)) —
    процентный кэп, резолвленный строкой («N% of Revenue»), а не числом.

    Заменяется ЛЮБОЙ Doc(key) в дереве, чьё значение в doc_facts разбирается
    parse_percent_of_statute — узел может стоять где угодно (min/max/add,
    не только вычитаемым, как в cap_related_party_basket), потому что
    источник агрегата ищется по ВСЕЙ метрике (root), а не по локальному
    поддереву узла. Любой из трёх гейтов parse_percent_of_statute или
    отсутствие однозначного одноимённого агрегата — узел остаётся
    прежним Doc, молча: как и у соседних переписываний, признание — по
    доступным данным, а не по угадыванию."""
    if metric_ast is None or not doc_facts:
        return metric_ast, False

    changed = False

    def rewrite(node):
        nonlocal changed
        if isinstance(node, Doc):
            replacement = _percent_cap_node(node.key, doc_facts, metric_ast)
            if replacement is not None:
                changed = True
                return replacement
            return node
        if not hasattr(node, "__dataclass_fields__"):
            return node
        updates = {}
        for name in node.__dataclass_fields__:
            value = getattr(node, name)
            if isinstance(value, tuple):
                updates[name] = tuple(rewrite(c) if hasattr(c, "__dataclass_fields__") else c for c in value)
            elif hasattr(value, "__dataclass_fields__"):
                updates[name] = rewrite(value)
        return dataclasses.replace(node, **updates) if updates else node

    out = rewrite(metric_ast)
    return (out, changed) if changed else (metric_ast, False)


# Признак поквартальности: квантор («любой»/«каждый», any/each) и слово
# «квартал»/quarter в пределах ограниченного разрыва слов, а не буквальная
# фраза целиком (раунд правок 1). Список готовых фраз («любом финансовом
# квартале» и т.п.) хрупок по построению: любое прилагательное, вставленное
# между квантором и «кварталом» («в любом ОТДЕЛЬНОМ финансовом квартале», «за
# каждый ЗАВЕРШЁННЫЙ финансовый квартал», «in any SINGLE fiscal quarter»),
# рвёт совпадение целиком, хотя смысл пункта не меняется — одна из ячеек
# приватного набора потерялась именно так: договор пишет «в любом ОТДЕЛЬНОМ
# финансовом квартале», а список фраз знал только «любом финансовом квартале».
#
# Основы вместо целых слов (тот же приём, что у dsl._SET_STEMS) снимают
# чувствительность к падежу и роду — «любой», «любом», «любого» и «любых»
# ловятся одной основой. Разрыв между квантором и «кварталом» ограничен
# ДВУМЯ словами — это несущее ограничение, а не украшение: без потолка образец
# склеивал бы квантор из одного предложения цитаты со словом «квартал» из
# совсем другого и квартализовал бы годовую метрику там, где пункт вообще не
# про кварталы. На публичном наборе договоров с поквартальными пунктами не
# было, поэтому правило там обязано молчать.
#
# Окно разрыва не пересекает границу предложения (раунд правок 2): цитаты
# пунктов в этих договорах многосоставны, точка внутри одной цитаты —
# наблюдаемая форма, а не крайний случай (см. историю ячейки, потерянной
# вставленным прилагательным между квантором и «кварталом» в том же
# предложении). Квантор в конце одного предложения и «квартал» в начале
# следующего оказались бы рядом чисто геометрически — окно ищется отдельно
# в каждом предложении, а не по цитате целиком.
_QUANT_STEMS_RU = ("любо", "кажд")
_QUANT_WORDS_EN = ("any", "each")
_QUARTER_STEMS = ("квартал", "quarter")
_MAX_GAP_WORDS = 2

# _WORD_RE/_SENTENCE_SPLIT_RE определены выше, рядом с
# flip_debt_incurrence_sign — тот же приём переиспользуется здесь.


def _is_quant_word(word: str) -> bool:
    return word.startswith(_QUANT_STEMS_RU) or word in _QUANT_WORDS_EN


def _is_quarter_word(word: str) -> bool:
    return word.startswith(_QUARTER_STEMS)


def _sentence_mentions_any_quarter(sentence: str) -> bool:
    words = _WORD_RE.findall(sentence)
    for i, word in enumerate(words):
        if not _is_quant_word(word):
            continue
        window = words[i + 1 : i + 1 + _MAX_GAP_WORDS + 1]
        if any(_is_quarter_word(w) for w in window):
            return True
    return False


def _mentions_any_quarter(quote: str) -> bool:
    """Пункт меряет «любой»/«каждый» квартал — по образцу, а не по фразе."""
    t = (quote or "").lower()
    return any(_sentence_mentions_any_quarter(s) for s in _SENTENCE_SPLIT_RE.split(t))


def _quarterize(node, n: int):
    """Копия узла, где каждый agg считает только квартал n.

    Годовой period() снимается: он и quarter() описывают один и тот же
    отчётный период, и оставленный period() ничего не изменил бы, но текст
    формулы в трейсе врал бы про то, что считается."""
    if isinstance(node, Agg):
        filters = tuple(f for f in node.filters if not isinstance(f, Period | Quarter))
        return dataclasses.replace(node, filters=filters + (Quarter(n=n),))
    if not hasattr(node, "__dataclass_fields__"):
        return node
    updates = {}
    for name in node.__dataclass_fields__:
        value = getattr(node, name)
        if isinstance(value, tuple):
            updates[name] = tuple(
                _quarterize(c, n) if hasattr(c, "__dataclass_fields__") else c for c in value
            )
        elif hasattr(value, "__dataclass_fields__"):
            updates[name] = _quarterize(value, n)
    return dataclasses.replace(node, **updates) if updates else node


def quarterly(metric_ast, quote: str, direction: str | None) -> tuple[object, bool]:
    """Годовая метрика → худший квартал, если пункт меряет любой квартал.

    Направление решает, какой квартал худший: у min-ковенанта («не ниже»)
    нарушение — самый маленький квартал, у max — самый большой. Годовой итог
    не лечит нарушенный квартал, и наоборот: на приватном наборе четыре
    ячейки посчитаны за год против поквартального ключа.

    Отношения не трогаем сознательно. Там, где квартальным является ТРИГГЕР
    («если выручка любого квартала ниже X, то отношение за период в целом не
    выше Y»), квартализация знаменателя испортила бы верную метрику; отделить
    один случай от другого по цитате нечем, а цена ошибки в эту сторону выше.
    """
    if not _mentions_any_quarter(quote):
        return metric_ast, False
    if direction not in ("min", "max"):
        return metric_ast, False
    # По всему дереву, не только по корню (раунд правок 2, Minor): извлечённая
    # формула вправе положить ratio() глубже, а квартализация ветки, где
    # знаменатель ей не рад, ломает верный ответ — отказ дешевле лишней ячейки.
    if any(isinstance(n, Ratio) for n in walk(metric_ast)):
        return metric_ast, False
    if any(isinstance(n, Quarter) for n in walk(metric_ast)):
        return metric_ast, False
    if not any(isinstance(n, Agg) for n in walk(metric_ast)):
        return metric_ast, False
    parts = tuple(_quarterize(metric_ast, n) for n in (1, 2, 3, 4))
    return (MinOf(args=parts) if direction == "min" else MaxOf(args=parts)), True


def apply_final(
    cellspec: dict,
    quote: str,
    direction: str | None = None,
    ebitda_needs_addback: bool = False,
    doc_facts: dict | None = None,
    doc_fact_quotes: dict | None = None,
) -> tuple[dict, list[dict]]:
    """Все финальные переписывания одним входом. cellspec не мутируется.

    Пустая цитата (эталонный режим) — ничего не переписываем: признак решения
    живёт в тексте пункта, и без него правка была бы гаданием.

    Сужение опекса применяется и к ТРИГГЕРУ (ревью финальной ветки, §5): выбор
    прочтения EBITDA — свойство договора, а не места формулы, и одна и та же
    EBITDA, посчитанная в метрике по статье, а в условии применимости по
    роллапу, отличалась бы на два порядка. Триггер решает, применяется ли
    ковенант вообще (несработавший даёт безусловный COMPLIANT), то есть это
    цена статуса, а не точности. Тот же принцип уже действует у
    solve._apply_ebitda_reading, который обрабатывает метрику и триггер
    наравне. Разовая корректировка EBITDA (задача 3, add_ebitda_addback) —
    того же рода свойство договора, и применяется к триггеру по тому же
    доводу.

    ebitda_needs_addback — уже готовый вердикт (facts_extract._quote_
    requires_addback), а не текст цитаты определения EBITDA: cellspec несёт
    его отдельным полем, потому что цитата определения — не то же самое, что
    quote пункта (последняя используется остальными переписываниями этой
    функции).

    Квартализация триггера, наоборот, НЕ делается — сознательно и по причине,
    описанной в докстринге quarterly: квартальным бывает именно условие
    («если выручка любого квартала ниже X»), и тогда квартализовать надо не
    его, а ничего.

    doc_facts/doc_fact_quotes (задача 4) — факты досье целиком (не только
    цитата пункта), нужны cap_related_party_basket: капнутая корзина
    резолвится по СОБСТВЕННОЙ цитате doc-ключа, а не по цитате всего пункта.
    resolve_percent_of_statute (процентный кэп) использует только doc_facts —
    своей цитаты doc-ключа ей не нужно, признак целиком в самом значении.
    По умолчанию None — старые вызовы без этих kwarg-ов ничего не переписывают."""
    if not quote or cellspec.get("metric_ast") is None:
        return cellspec, []
    alarms: list[dict] = []
    ast = cellspec["metric_ast"]

    ast, narrowed = narrow_opex(ast, quote)
    if narrowed:
        alarms.append(
            {"kind": "opex_rollup_narrowed", "from": "OPEX_TOTAL", "to": "OTHER_OPEX", "target": "metric"}
        )

    ast, addback_applied = add_ebitda_addback(ast, ebitda_needs_addback)
    if addback_applied:
        alarms.append({"kind": "ebitda_addback_applied", "target": "metric"})

    trigger = cellspec.get("trigger_ast")
    trigger_narrowed = False
    trigger_addback_applied = False
    if trigger is not None:
        trigger, trigger_narrowed = narrow_opex(trigger, quote)
        if trigger_narrowed:
            alarms.append(
                {
                    "kind": "opex_rollup_narrowed",
                    "from": "OPEX_TOTAL",
                    "to": "OTHER_OPEX",
                    "target": "trigger",
                }
            )
        trigger, trigger_addback_applied = add_ebitda_addback(trigger, ebitda_needs_addback)
        if trigger_addback_applied:
            alarms.append({"kind": "ebitda_addback_applied", "target": "trigger"})

    ast, widened = widen_related_party(ast, quote)
    if widened:
        alarms.append({"kind": "related_party_widened", "to": "ALL", "target": "metric"})

    ast, basket_capped = cap_related_party_basket(ast, doc_facts, doc_fact_quotes)
    if basket_capped:
        alarms.append({"kind": "related_party_basket_capped", "target": "metric"})

    ast, percent_cap_resolved = resolve_percent_of_statute(ast, doc_facts)
    if percent_cap_resolved:
        alarms.append({"kind": "doc_percent_of_statute_resolved", "target": "metric"})

    ast, sign_flipped = flip_debt_incurrence_sign(ast, quote)
    if sign_flipped:
        alarms.append({"kind": "financing_sign_flipped", "from": "out", "to": "in", "target": "metric"})

    # Порядок важен: квартализация копирует поддерево четырежды, и узкий опекс
    # (расширение категории связанных сторон, переворот знака привлечённой
    # задолженности) обязаны быть выбраны ДО копирования — иначе они
    # чинились бы в четырёх местах.
    ast, quartered = quarterly(ast, quote, direction)
    if quartered:
        alarms.append({"kind": "metric_quarterized", "direction": direction})

    if not alarms:
        return cellspec, []
    out = {**cellspec, "metric_ast": ast, "metric_text": unparse(ast)}
    if trigger_narrowed or trigger_addback_applied:
        out["trigger_ast"] = trigger
    return out, alarms
