"""Переписывание финального AST ячейки перед расчётом.

Здесь живут правки, которые нельзя доверить модели, потому что цена ошибки
несимметрична, а признак — механический. Модуль сознательно не трогает
промпты: ключ LLM-кэша считается от текста промпта, и правка промпта стоила бы
всей кассеты, то есть возможности мерить изменение офлайн.
"""

import dataclasses

from dsl import Agg, Sub, unparse

# Маркеры того, что договор перечисляет статьи операционных расходов, то есть
# понимает их ШИРОКО. Основы, не полные слова: падежи и вёрстка разные, а
# основа одна. Два маркера, а не один: одиночное упоминание аренды в пункте
# про EBITDA — это чаще знаменатель ковенанта, чем перечисление статей.
_ARTICLE_MARKERS = (
    "оплат",
    "труд",
    "фот",
    "аренд",
    "коммунал",
    "налог",
    "страхов",
    "консультац",
    "маркетинг",
    "payroll",
    "rent",
    "utilit",
    "insur",
    "consult",
    "marketing",
)

# Маркеры прямого широкого прочтения: договор сам говорит «все операционные».
_TOTAL_MARKERS = (
    "всех операционных",
    "все операционные",
    "всеми операционными",
    "совокупных операционных",
    "совокупные операционные",
    "total operating expense",
    "all operating expense",
)

_MIN_ARTICLES_FOR_ROLLUP = 2


def _quote_reads_broadly(quote: str) -> bool:
    t = (quote or "").lower()
    if any(m in t for m in _TOTAL_MARKERS):
        return True
    return sum(1 for m in _ARTICLE_MARKERS if m in t) >= _MIN_ARTICLES_FOR_ROLLUP


def narrow_opex(metric_ast, quote: str) -> tuple[object, bool]:
    """EBITDA считается по статье, если договор не сказал обратного явно.

    Роллап OPEX_TOTAL остаётся законным вторым прочтением, но перестаёт быть
    прочтением ПО УМОЛЧАНИЮ: на приватном наборе он не был верен ни разу, а
    цена ошибки в его сторону — двукратный порядок в знаменателе EBITDA
    (355 млн против 3.2 млн у одного заёмщика). Переписывается только
    EBITDA-подвыражение sub(выручка, опекс) — ровно та же граница, что у
    solve._apply_ebitda_reading: ковенант о доле консультационных в
    операционных расходах оперирует своим роллапом независимо."""
    if _quote_reads_broadly(quote):
        return metric_ast, False

    changed = False

    def rewrite(node):
        nonlocal changed
        if (
            isinstance(node, Sub)
            and isinstance(node.a, Agg)
            and isinstance(node.b, Agg)
            and node.a.category == "REVENUE"
            and node.b.category == "OPEX_TOTAL"
        ):
            changed = True
            return Sub(a=node.a, b=dataclasses.replace(node.b, category="OTHER_OPEX"))
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


def apply_final(cellspec: dict, quote: str) -> tuple[dict, list[dict]]:
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

    if not alarms:
        return cellspec, []
    return {**cellspec, "metric_ast": ast, "metric_text": unparse(ast)}, alarms
