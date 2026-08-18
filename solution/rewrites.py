"""Переписывание финального AST ячейки перед расчётом.

Здесь живут правки, которые нельзя доверить модели, потому что цена ошибки
несимметрична, а признак — механический. Модуль сознательно не трогает
промпты: ключ LLM-кэша считается от текста промпта, и правка промпта стоила бы
всей кассеты, то есть возможности мерить изменение офлайн.
"""

import dataclasses
import re

from dsl import Add, Agg, MaxOf, MinOf, Period, Quarter, Ratio, Sub, unparse, walk

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
_QUANT_STEMS_RU = ("любо", "кажд")
_QUANT_WORDS_EN = ("any", "each")
_QUARTER_STEMS = ("квартал", "quarter")
_MAX_GAP_WORDS = 2

_WORD_RE = re.compile(r"[\w-]+", re.UNICODE)


def _is_quant_word(word: str) -> bool:
    return word.startswith(_QUANT_STEMS_RU) or word in _QUANT_WORDS_EN


def _is_quarter_word(word: str) -> bool:
    return word.startswith(_QUARTER_STEMS)


def _mentions_any_quarter(quote: str) -> bool:
    """Пункт меряет «любой»/«каждый» квартал — по образцу, а не по фразе."""
    words = _WORD_RE.findall((quote or "").lower())
    for i, word in enumerate(words):
        if not _is_quant_word(word):
            continue
        window = words[i + 1 : i + 1 + _MAX_GAP_WORDS + 1]
        if any(_is_quarter_word(w) for w in window):
            return True
    return False


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
    if isinstance(metric_ast, Ratio):
        return metric_ast, False
    if any(isinstance(n, Quarter) for n in walk(metric_ast)):
        return metric_ast, False
    if not any(isinstance(n, Agg) for n in walk(metric_ast)):
        return metric_ast, False
    parts = tuple(_quarterize(metric_ast, n) for n in (1, 2, 3, 4))
    return (MinOf(args=parts) if direction == "min" else MaxOf(args=parts)), True


def apply_final(cellspec: dict, quote: str, direction: str | None = None) -> tuple[dict, list[dict]]:
    """Все финальные переписывания одним входом. cellspec не мутируется.

    Пустая цитата (эталонный режим) — ничего не переписываем: признак решения
    живёт в тексте пункта, и без него правка была бы гаданием."""
    if not quote or cellspec.get("metric_ast") is None:
        return cellspec, []
    alarms: list[dict] = []
    ast = cellspec["metric_ast"]

    ast, narrowed = narrow_opex(ast, quote)
    if narrowed:
        alarms.append({"kind": "opex_rollup_narrowed", "from": "OPEX_TOTAL", "to": "OTHER_OPEX"})

    # Порядок важен: квартализация копирует поддерево четырежды, и узкий опекс
    # обязан быть выбран ДО копирования — иначе он чинился бы в четырёх местах.
    ast, quartered = quarterly(ast, quote, direction)
    if quartered:
        alarms.append({"kind": "metric_quarterized", "direction": direction})

    if not alarms:
        return cellspec, []
    return {**cellspec, "metric_ast": ast, "metric_text": unparse(ast)}, alarms
