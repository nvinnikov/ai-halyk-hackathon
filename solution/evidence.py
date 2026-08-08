"""Улика (5.6): транзакция, чья переклассификация, включение, исключение
или исправление приводит к нарушению. Вкладчик в агрегат уликой не бывает.

D собирается из документальных решений, контрфактуал — откат именно этого
решения по его типу. Ровно один переворачивающий кандидат → улика.
Кандидат вне D не бывает правильным ответом; внутри D щедрость бесплатна.
"""

from dsl import CounterpartyIn, uses_ledger, walk
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


def find(raw_rows, facts, cellspec, status) -> tuple[str | None, list[dict]]:
    """(evidence_txn_id, trace): ровно один переворачивающий кандидат → улика,
    иначе null — и это правильный ответ, а не пробел."""
    if status != "BREACH":
        return None, []
    trace = []
    flippers = []
    for cand in candidates(raw_rows, facts, cellspec):
        alt_status, _ = compute(
            raw_rows,
            facts,
            cellspec,
            overrides=cand["overrides"],
            set_exclude=frozenset(cand["set_exclude"]),
        )
        flipped = alt_status != status
        trace.append({**cand, "flipped": flipped})
        if flipped:
            flippers.append(cand["txn"])
    unique = sorted(set(flippers))
    return (unique[0] if len(unique) == 1 else None), trace
