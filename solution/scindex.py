"""Индекс txn_id → scenario_id → account_id (5.2).

Единственное место разбора txn_id. Целевые сценарии задаёт шаблон;
всё прочее — фон, который считается, но не является ошибкой.
"""

import re
from collections import defaultdict

INDEX_VERSION = 1


def build_index(rows: list[dict], targets: list[str]) -> dict:
    """Построить индекс сценариев и счётов из строк леджера.

    Ищет целевой id на границах небуквенно-цифровых символов в txn_id.
    Одна целевая ссылка на сценарий → успех; иного числа → алярм.
    Фоновые счета (не целевые сценарии) подсчитываются, но не ошибка.

    Args:
        rows: список строк леджера с txn_id и account_id
        targets: список целевых sценариев (из шаблона)

    Returns:
        Словарь с индексами, фоновой статистикой и алярмами.
    """
    target_set = set(targets)
    links: dict[str, set[str]] = defaultdict(set)
    background_accounts: set[str] = set()
    background_rows = 0
    ambiguous_txns = []

    for r in rows:
        # Поиск целевого id на границах небуквенно-цифровых символов
        hits = sorted(
            sc
            for sc in target_set
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(sc)}(?![A-Za-z0-9])", r["txn_id"])
        )

        if len(hits) == 1:
            links[hits[0]].add(r["account_id"])
        elif len(hits) > 1:
            # Несколько целевых id в одной строке
            ambiguous_txns.append(r["txn_id"])
            background_accounts.add(r["account_id"])
            background_rows += 1
        else:
            # Ни один целевой id не найден
            background_accounts.add(r["account_id"])
            background_rows += 1

    alarms = []

    # Алярмы на ambiguous_txn
    for txn in ambiguous_txns:
        alarms.append({"kind": "ambiguous_txn", "txn_id": txn})

    s2a = {}
    for sc in sorted(target_set):
        accounts = sorted(links.get(sc, ()))
        if len(accounts) == 1:
            s2a[sc] = accounts[0]
        else:
            alarms.append({"kind": "index_cardinality", "scenario": sc, "accounts": accounts})

    a2s: dict[str, str] = {}
    for sc in sorted(s2a):
        acc = s2a[sc]
        if acc in a2s:
            alarms.append({"kind": "shared_account", "account": acc, "scenarios": sorted([a2s[acc], sc])})
        a2s[acc] = sc

    total = len(rows)
    return {
        "scenario_to_account": s2a,
        "account_to_scenario": a2s,
        "background": {
            "accounts": len(background_accounts),
            "rows": background_rows,
            "row_share": round(background_rows / total, 4) if total else 0.0,
        },
        "alarms": alarms,
    }
