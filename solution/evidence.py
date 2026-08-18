"""Улика (5.6): транзакция, чья переклассификация, включение, исключение или
исправление приводит к нарушению.

Спека определяет улику через документальное решение, и множество D из таких
решений остаётся ПЕРВЫМ по приоритету. Но правила скоринга асимметричны: при
null в ключе присланное значение не учитывается вовсе, поэтому непустая
догадка либо приносит долю оценки за улику, либо не меняет ничего —
отрицательной стороны у неё нет. Прежняя формулировка «ровно один
переворачивающий → улика, иначе null» была верна как прочтение спеки и
неверна как ставка: на приватном наборе она отдала заметную долю BREACH-ячеек
даже там, где статус и значение уже совпадали с ответом. Поэтому на BREACH с
метрикой, читающей леджер, улика теперь есть всегда: сначала документальные
решения, затем любая читаемая строка, переворачивающая вердикт, затем
крупнейшая читаемая строка.
"""

from decimal import Decimal

from dsl import Agg, CounterpartyIn, uses_ledger, walk
from engine import is_related, prepare_rows, tokens
from interp import Ctx, check_trigger, evaluate, verdict


def compute(raw_rows, facts, cellspec, overrides=None, set_exclude=frozenset()):
    """Статус и значение ячейки с применёнными контрфактуалами.

    Подготовка строк (prepare_rows) происходит здесь: контрфактуал должен
    видеть сырые строки, в подготовленных отсечённой операции уже нет."""
    rows = prepare_rows(raw_rows, facts, overrides)
    ctx = Ctx(rows=rows, facts=facts, set_exclude=set_exclude)
    res = evaluate(cellspec["metric_ast"], ctx)
    if not check_trigger(cellspec["trigger_ast"], ctx):
        return "COMPLIANT", res
    status, _ = verdict(res, cellspec["direction"], cellspec["limit"])
    return status, res


def _party_sets(cellspec) -> list[str]:
    return sorted(
        {
            n.setname
            for n in walk(cellspec["metric_ast"])
            if isinstance(n, CounterpartyIn) and isinstance(n.setname, str)
        }
    )


def candidates(raw_rows, facts, cellspec) -> list[dict]:
    """Множество D: транзакции, чьё членство/сумма — следствие документального
    решения. doc()-метрика без чтения леджера кандидатов не имеет."""
    if not uses_ledger(cellspec["metric_ast"]):
        return []
    out = []
    rows = prepare_rows(raw_rows, facts)
    by_txn = {r["txn_id"]: r for r in rows}

    # Кандидаты порождаются для КАЖДОЙ совпавшей реклассификации, включая
    # проигравшие тай-брейк специфичности в prepare_rows (ревью PR #9, 22-я
    # волна). Это не рассинхрон: откат проигравшей — no-op (победитель
    # остаётся, вердикт не переворачивается, кандидат отсеивается), а откат
    # победителя честно моделирует «этого решения нет» — строку
    # переклассифицирует следующий по специфичности документ, а не сырая
    # категория леджера. Возврат к сырой категории игнорировал бы второй
    # документ, который при отсутствии первого действует.
    for i, rc in enumerate(facts.get("reclass", [])):
        for r in rows:
            hit = rc.get("txn") == r["txn_id"] or (
                rc.get("counterparty") and tokens(rc["counterparty"]) == tokens(r["counterparty"])
            )
            if hit:
                out.append(
                    {
                        "txn": r["txn_id"],
                        "decision_type": "reclass",
                        "quote": rc.get("quote", ""),
                        "overrides": {"undo_reclass": {i}},
                        "set_exclude": [],
                    }
                )

    for setname in _party_sets(cellspec):
        parties = facts.get(setname, [])
        pquotes = (
            facts.get("related_quotes", {})
            if setname == "related_parties"
            else facts.get("subsidiary_quotes", {})
        )
        for r in rows:
            if r["amt"] < 0 and is_related(r["counterparty"], parties):
                matched = sorted(p for p in parties if is_related(r["counterparty"], [p]))
                out.append(
                    {
                        "txn": r["txn_id"],
                        "decision_type": "inclusion",
                        "quote": "; ".join(pquotes.get(p, "") for p in matched),
                        "overrides": None,
                        "set_exclude": [r["txn_id"]],
                    }
                )

    for txn in sorted(facts.get("exclude", [])):
        out.append(
            {
                "txn": txn,
                "decision_type": "exclusion",
                "quote": facts.get("exclude_quotes", {}).get(txn, ""),
                "overrides": {"undo_exclude": {txn}},
                "set_exclude": [],
            }
        )

    for txn in sorted(facts.get("amount_override", {})):
        if txn in by_txn:
            out.append(
                {
                    "txn": txn,
                    "decision_type": "amount_fix",
                    "quote": facts.get("override_quotes", {}).get(txn, ""),
                    "overrides": {"undo_override": {txn}},
                    "set_exclude": [],
                }
            )

    out.sort(key=lambda c: (c["txn"], c["decision_type"]))
    return out


def reading_rows(metric_ast, rows: list[dict], facts: dict) -> list[dict]:
    """Строки, которые метрика действительно читает.

    Кандидат вне множества чтения не может перевернуть вердикт, и предлагать
    его как улику — заведомо мимо: аренда не бывает доказательством по
    ковенанту о капитальных затратах. Объединение по всем agg-узлам с учётом
    знака и фильтров каждого; порядок — по txn_id."""
    from interp import row_filter
    from taxonomy import expand

    ctx = Ctx(rows=rows, facts=facts)
    seen: dict[str, dict] = {}
    for node in walk(metric_ast):
        if not isinstance(node, Agg):
            continue
        cats = expand(node.category)
        keep = row_filter(node.filters, ctx)
        for r in rows:
            if r["cat"] not in cats or not keep(r):
                continue
            if node.sign == "out" and r["amt"] >= 0:
                continue
            if node.sign == "in" and r["amt"] <= 0:
                continue
            seen[r["txn_id"]] = r
    return [seen[k] for k in sorted(seen)]


def _ledger_candidates(raw_rows, facts, cellspec) -> list[dict]:
    """Кандидаты второго круга: каждая читаемая строка, снятая целиком."""
    rows = prepare_rows(raw_rows, facts)
    return [
        {
            "txn": r["txn_id"],
            "decision_type": "ledger_row",
            "quote": "",
            "overrides": None,
            "set_exclude": [r["txn_id"]],
            "amt": r["amt"],
        }
        for r in reading_rows(cellspec["metric_ast"], rows, facts)
    ]


_DECISION_RANK = {"reclass": 0, "amount_fix": 1, "exclusion": 2, "inclusion": 3, "ledger_row": 4}


def find(raw_rows, facts, cellspec, status) -> tuple[str | None, list[dict]]:
    """(evidence_txn_id, trace). На BREACH с метрикой по леджеру — всегда непусто.

    Порядок предпочтения: документальное решение, переворачивающее вердикт →
    любая читаемая строка, переворачивающая вердикт → крупнейшая читаемая
    строка. Внутри каждой ступени — детерминированно: сначала тип решения,
    затем убывание модуля суммы, затем txn_id."""
    if status != "BREACH" or not uses_ledger(cellspec["metric_ast"]):
        return None, []
    rows = prepare_rows(raw_rows, facts)
    amounts = {r["txn_id"]: abs(r["amt"]) for r in rows}
    trace = []
    flippers = []
    seen = set()
    for cand in candidates(raw_rows, facts, cellspec) + _ledger_candidates(raw_rows, facts, cellspec):
        key = (cand["txn"], cand["decision_type"])
        if key in seen:
            continue
        seen.add(key)
        alt_status, _ = compute(
            raw_rows,
            facts,
            cellspec,
            overrides=cand["overrides"],
            set_exclude=frozenset(cand["set_exclude"]),
        )
        flipped = alt_status != status
        trace.append({k: v for k, v in cand.items() if k != "amt"} | {"flipped": flipped})
        if flipped:
            flippers.append(cand)

    def rank(c):
        return (
            _DECISION_RANK.get(c["decision_type"], 9),
            -amounts.get(c["txn"], Decimal(0)),
            c["txn"],
        )

    if flippers:
        return sorted(flippers, key=rank)[0]["txn"], trace
    read = reading_rows(cellspec["metric_ast"], rows, facts)
    if not read:
        return None, trace
    biggest = sorted(read, key=lambda r: (-abs(r["amt"]), r["txn_id"]))[0]
    return biggest["txn_id"], trace
