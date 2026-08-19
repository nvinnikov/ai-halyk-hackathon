"""Переписывание финального AST ячейки перед расчётом.

Здесь живут правки, которые нельзя доверить модели, потому что цена ошибки
несимметрична, а признак — механический. Модуль сознательно не трогает
промпты: ключ LLM-кэша считается от текста промпта, и правка промпта стоила бы
всей кассеты, то есть возможности мерить изменение офлайн.
"""

import dataclasses
import re

from dsl import Add, Agg, CounterpartyIn, Doc, MaxOf, MinOf, Period, Quarter, Ratio, Sub, unparse, walk
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
    его, а ничего."""
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
