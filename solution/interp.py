"""Интерпретатор DSL: считает всегда, сравнивает со знаком, модуль — при выводе.

Деление на ноль — помеченное значение и алярм, а не пропуск вычисления (5.7).
Отрицательный знаменатель (EBITDA ≤ 0) — алярм и BREACH при direction=max:
знаковое сравнение отрицательного числителя с пороговым значением требует
осторожности с интерпретацией знака.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from dsl import (
    Add,
    Agg,
    Cmp,
    Const,
    CounterpartyIn,
    DescContains,
    Doc,
    MaxOf,
    MinAmount,
    MinOf,
    Mul,
    Period,
    Quarter,
    Ratio,
    Sub,
    TxnIn,
)
from engine import agg, is_related


@dataclass(frozen=True)
class Ctx:
    rows: list
    facts: dict
    set_exclude: frozenset = frozenset()


@dataclass(frozen=True)
class EvalResult:
    value: Decimal
    flags: frozenset = field(default_factory=frozenset)


def _quarter_months(n: int) -> tuple[str, ...]:
    return tuple(f"{m:02d}" for m in range(3 * n - 2, 3 * n + 1))


def _pred(filters: tuple, ctx: Ctx):
    def check(r) -> bool:
        for f in filters:
            if isinstance(f, Period):
                if not (f.frm <= r["date"] <= f.to):
                    return False
            elif isinstance(f, Quarter):
                if r["date"][5:7] not in _quarter_months(f.n):
                    return False
            elif isinstance(f, CounterpartyIn):
                if r["txn_id"] in ctx.set_exclude:
                    return False
                parties = list(f.setname) if isinstance(f.setname, tuple) else ctx.facts.get(f.setname, [])
                if not is_related(r["counterparty"], parties):
                    return False
            elif isinstance(f, TxnIn):
                if r["txn_id"] not in f.ids:
                    return False
            elif isinstance(f, MinAmount):
                if abs(r["amt"]) < f.x:
                    return False
            elif isinstance(f, DescContains):
                if f.s.lower() not in r["description"].lower():
                    return False
        return True

    return check


def evaluate(node, ctx: Ctx) -> EvalResult:
    if isinstance(node, Agg):
        return EvalResult(agg(ctx.rows, node.category, node.sign, _pred(node.filters, ctx)))
    if isinstance(node, Doc):
        return EvalResult(Decimal(str(ctx.facts["doc_facts"][node.key])))
    if isinstance(node, Const):
        return EvalResult(node.value)
    if isinstance(node, Ratio):
        num, den = evaluate(node.num, ctx), evaluate(node.den, ctx)
        flags = set(num.flags | den.flags)
        if den.value == 0:
            flags.add("zero_denominator")
            # Знак числителя — в флаг: подстановка нуля его стирает, а вердикт
            # min-метрики от него зависит (∞ только при положительном
            # числителе; −EBITDA/0 — это −∞, ревью PR #9, 24-я волна).
            if num.value < 0:
                flags.add("zero_den_negative_num")
            elif num.value == 0:
                flags.add("zero_den_zero_num")
            return EvalResult(Decimal(0), frozenset(flags))
        if den.value < 0:
            flags.add("negative_denominator")
        return EvalResult(num.value / den.value, frozenset(flags))
    if isinstance(node, Sub):
        a, b = evaluate(node.a, ctx), evaluate(node.b, ctx)
        return EvalResult(a.value - b.value, a.flags | b.flags)
    if isinstance(node, Mul):
        a, b = evaluate(node.a, ctx), evaluate(node.b, ctx)
        return EvalResult(a.value * b.value, a.flags | b.flags)
    if isinstance(node, Add | MaxOf | MinOf):
        parts = [evaluate(a, ctx) for a in node.args]
        merged = frozenset().union(*(p.flags for p in parts))
        vals = [p.value for p in parts]
        if isinstance(node, Add):
            value = sum(vals, Decimal(0))
        else:
            value = max(vals) if isinstance(node, MaxOf) else min(vals)
        return EvalResult(value, merged)
    raise TypeError(f"не выражение: {node!r}")


def check_trigger(node, ctx: Ctx) -> bool:
    if node is None:
        return True
    assert isinstance(node, Cmp)
    a, b = evaluate(node.a, ctx).value, evaluate(node.b, ctx).value
    return {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[node.op]


def verdict(res: EvalResult, direction: str, limit: Decimal) -> tuple[str, list[str]]:
    alarms = sorted(res.flags)
    if direction == "max" and res.flags & {"negative_denominator", "zero_denominator"}:
        # Ноль в знаменателе — бесконечное отношение, а не нулевое: подставленный
        # evaluate ноль дал бы ложный COMPLIANT при любом положительном пороге.
        return "BREACH", alarms
    if direction == "max":
        return ("BREACH" if res.value > limit else "COMPLIANT"), alarms
    if "zero_denominator" in res.flags and not res.flags & {
        "zero_den_negative_num",
        "zero_den_zero_num",
    }:
        # Нулевой знаменатель у min-метрики — «покрывать нечего»: отношение
        # бесконечно, ∞ не меньше порога → COMPLIANT. Подставленный evaluate
        # ноль дал бы ложный BREACH при любом положительном пороге (ревью
        # PR #9, 22-я волна). Только при положительном числителе: −EBITDA/0 —
        # это −∞, а 0/0 не определён — оба падают в общий BREACH ниже (24-я
        # волна). negative_denominator не трогаем: там значение действительно
        # отрицательное и вердикт совпадает с истинным.
        return "COMPLIANT", alarms
    # Для min подставленный ноль ниже любого положительного порога — BREACH:
    # неопределённая метрика трактуется как нарушение, не как соблюдение.
    return ("BREACH" if res.value < limit else "COMPLIANT"), alarms
