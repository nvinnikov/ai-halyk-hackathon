"""Переписывание финального AST ячейки перед расчётом.

Здесь живут правки, которые нельзя доверить модели, потому что цена ошибки
несимметрична, а признак — механический. Модуль сознательно не трогает
промпты: ключ LLM-кэша считается от текста промпта, и правка промпта стоила бы
всей кассеты, то есть возможности мерить изменение офлайн.
"""

import dataclasses

from dsl import Add, Agg, Sub, unparse

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
