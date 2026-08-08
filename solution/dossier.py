"""Сшивка досье: маршрутизированные документы группируются по целевым счетам,
среди редакций одного типа остаётся действующая (5.2.1).

Документы адресуются doc_hash, а не именем файла: базовые имена во вложенных
каталогах приватного архива могут коллидировать. Маршрутизация независима по
файлам и идёт пулом потоков (SOLVE_WORKERS, дефолт 4) — ограничитель здесь
rate limit LLM; результаты собираются в детерминированном порядке.

Fail-open на двух уровнях (задача 24, ревью раунда 1): сбой чтения/
маршрутизации одного документа (LLM, vision на слепой странице, битый PDF) не
роняет весь пул — документ уходит в карантин с алярмом routing_failed;
сбой сборки досье одного заёмщика (например, vision внутри full_text при
подстановке активных документов) не роняет остальных заёмщиков — его досье
приходит пустым с алярмом dossier_build_failed. Раньше оба сбоя всплывали как
исключение из build_dossiers() целиком и убивали solve.main() до записи
скелета.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pdftext import doc_hash
from route import full_text, route_doc
from stages import artifact

# v6 — активационный бамп (2026-08-08): досье собиралось из route-артефактов
# v1; после ROUTE_VERSION=2 (META/WHOSE по тексту без футера) кэшированное
# досье устарело по входу. v5: алярмы карантина в артефакте досье.
DOSSIER_VERSION = 7
# v7 — кумулятивные типы больше не отдают замененные редакции: рабочий
# документ с маркером «заменён окончательным отчётом» вносил в досье
# предварительную реклассификацию операции и искажал две ячейки заёмщика.
_EDITION_RANK = {"final": 0, "unmarked": 1, "draft": 2, "superseded": 3}
# Редакционная фильтрация — только для перевыпускаемых целиком типов. Отчёты
# и записки кумулятивны: каждый несёт своё документальное решение, и отброс
# по дате терял бы реклассификации и исправления сумм (правка по замеру).
CUMULATIVE_TYPES = frozenset({"audit_report", "treasury_memo", "financial_notes"})


def _neg_date(date: str) -> str:
    d = date or "0000-00-00"
    return "".join(chr(0xFFFF - ord(c)) for c in d)  # сортировка по убыванию даты


def _pick_active(docs: list[dict]) -> tuple[dict | None, list[dict]]:
    """Маркер перебивает дату: сначала ранг редакции, затем дата по убыванию.

    Документ с маркером superseded не бывает действующим — даже единственный:
    пометка «заменён» означает, что действующей редакции в досье нет."""
    alive = [d for d in docs if d["edition"] != "superseded"]
    dead = [d for d in docs if d["edition"] == "superseded"]
    if not alive:
        return None, [{"file": d["file"], "reason": "superseded_edition", "kept": None} for d in dead]
    ranked = sorted(alive, key=lambda d: (_EDITION_RANK[d["edition"]], _neg_date(d["date"]), d["file"]))
    active, rejected = ranked[0], []
    for d in ranked[1:]:
        reason = (
            "edition_marker"
            if _EDITION_RANK[d["edition"]] != _EDITION_RANK[active["edition"]]
            else "superseded_by_date"
        )
        rejected.append({"file": d["file"], "reason": reason, "kept": active["file"]})
    rejected.extend({"file": d["file"], "reason": "superseded_edition", "kept": active["file"]} for d in dead)
    return active, rejected


def _route_or_quarantine(wd: Path, p: Path, targets: list[str], all_accounts: list[str] | None) -> dict:
    """route_doc ловит SchemaRejected вокруг своих LLM-вызовов, но не бюджет
    (llm.BudgetExhausted), не сетевые/авторизационные сбои и не vision внутри
    full_text() при чтении слепой страницы. Сбой одного документа не должен
    ронять список результатов для всех остальных — документ уходит в карантин
    с алярмом routing_failed вместо исключения из pool.map()."""
    try:
        return route_doc(wd, p, targets, all_accounts)
    except Exception as exc:
        # Видимость сразу (ревью PR #9, 9-я волна): route-артефакт при
        # исключении не пишется, и без print сбой маршрутизации (в первую
        # очередь BudgetExhausted) не оставлял следов ни в stdout, ни в
        # run-report.
        print(f"ALARM routing_failed {p.name}: {exc!r}", flush=True)
        try:
            h = doc_hash(p)
        except Exception:
            h = ""
        return {
            "file": p.name,
            "doc_hash": h,
            "account_id": None,
            "doc_type": "unrouted",
            "date": "",
            "edition": "unmarked",
            "mentions": [],
            "mentions_nontarget": [],
            "quarantined": True,
            "quarantine_reason": "routing_failed",
            "alarms": [{"kind": "routing_failed", "file": p.name, "error": repr(exc)}],
            "routing_quote": "",
        }


def build_dossiers(
    wd: Path, pdfs: list[Path], index: dict, all_accounts: list[str] | None = None
) -> dict[str, dict]:
    """all_accounts — все счета леджера: по ним route отличает фоновый документ
    от документа без счетов вовсе (карантин без алярма против карантина с ним)."""
    targets = sorted(index["account_to_scenario"])
    ordered = sorted(pdfs, key=lambda x: (x.name, str(x)))
    workers = int(os.environ.get("SOLVE_WORKERS", "4"))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda p: _route_or_quarantine(wd, p, targets, all_accounts), ordered))
    routed = [r for r in results if not r["quarantined"]]
    quarantined = [r for r in results if r["quarantined"]]
    by_hash = {doc_hash(p): p for p in ordered}

    # routing_failed — транзиентный сбой (бюджет, сеть, CassetteMiss), а не
    # свойство архива: досье, собранное при таком сбое, заведомо неполно.
    # stages.artifact инвалидируется только по версии, поэтому записанный
    # деградированный артефакт пережил бы перезапуск после устранения причины
    # (route/*.json при исключении не пишется — маршрутизация повторится, а
    # досье осталось бы старым). Деградированный результат не кэшируем: прогон
    # не падает, но следующий запуск собирает досье заново (ревью PR #9, 20-я
    # волна; тот же механизм залипания уже жёг прогон через
    # facts_extraction_failed).
    # meta_extraction_failed — тоже деградация маршрутизации (SchemaRejected
    # на META → карантин non_client_doc_type): route-артефакт при нём не
    # кэшируется (cache_if, 23-я волна), а досье без этого пункта кэшировалось
    # бы и не самовосстанавливалось (ревью PR #9, 28-я волна).
    _degraded_kinds = {"routing_failed", "meta_extraction_failed"}
    degraded = any(a.get("kind") in _degraded_kinds for q in quarantined for a in q.get("alarms", []))

    out: dict[str, dict] = {}
    for acc in targets:

        def build(acc=acc) -> dict:
            mine = [r for r in routed if r["account_id"] == acc]
            docs, docs_rejected = [], []
            by_type: dict[str, list[dict]] = {}
            for r in mine:
                by_type.setdefault(r["doc_type"], []).append(r)
            for dtype in sorted(by_type):
                rej: list[dict] = []
                if dtype in CUMULATIVE_TYPES:
                    # Кумулятивные типы: в досье попадают все документы, их
                    # факты сливает facts_extract. Кумулятивность — про то, что
                    # редакции не выбирают по дате; право читать замененный
                    # документ она не даёт. Рабочий документ с маркером
                    # «заменён окончательным отчётом» несёт предварительное
                    # решение по классификации, и применение такого решения
                    # искажает ковенант ровно так же, как устаревшая редакция
                    # договора. Инвариант тот же, что в _pick_active: документ
                    # с маркером superseded не бывает действующим — даже
                    # единственный.
                    ordered_type = sorted(by_type[dtype], key=lambda d: (d["date"], d["file"]))
                    actives = [d for d in ordered_type if d["edition"] != "superseded"]
                    kept = actives[0]["file"] if actives else None
                    rej = [
                        {"file": d["file"], "reason": "superseded_edition", "kept": kept}
                        for d in ordered_type
                        if d["edition"] == "superseded"
                    ]
                else:
                    active, rej = _pick_active(by_type[dtype])
                    actives = [active] if active is not None else []
                for active in actives:
                    docs.append(
                        {
                            "file": active["file"],
                            "doc_type": dtype,
                            "date": active["date"],
                            "text": full_text(wd, by_hash[active["doc_hash"]]),
                        }
                    )
                docs_rejected.extend(rej)
            return {
                "account_id": acc,
                "scenario_id": index["account_to_scenario"][acc],
                "docs": docs,
                # Причины отказов и карантина потребляет borrower-трейс (задача 17).
                "docs_rejected": docs_rejected,
                "quarantined": [
                    {"file": q["file"], "reason": q.get("quarantine_reason")}
                    for q in sorted(quarantined, key=lambda x: x["file"])
                ],
                # Алярмы карантина (routing_failed и т.п.) — в артефакт
                # досье: их читают сканеры run-report/sanity/invariants
                # (ревью PR #9, 9-я волна — раньше алярм создавался и
                # нигде не потреблялся).
                "alarms": sorted(
                    (a for q in quarantined for a in q.get("alarms", [])),
                    key=lambda a: (a.get("file", ""), a.get("kind", "")),
                ),
            }

        try:
            # cache_if, а не обход artifact() целиком (ревью PR #9, 23-я
            # волна): при деградированной маршрутизации уже закэшированное
            # ХОРОШЕЕ досье с прошлого прогона читается как обычно —
            # перезапуск после сбоя не хуже сохранённого состояния;
            # блокируется только запись свежесобранного неполного результата.
            out[acc] = artifact(
                wd / "dossier" / f"{acc}.json",
                DOSSIER_VERSION,
                build,
                cache_if=lambda _d: not degraded,
            )
        except Exception as exc:
            # Сбой чтения текста документа (например, vision на слепой
            # странице) для этого заёмщика не должен рушить досье остальных:
            # заёмщик остаётся без документов, но с алярмом. Пустое досье —
            # мимо artifact(): исключение из build() внутри artifact не даёт
            # записи, а этот dict не должен закрепить деградацию на диске.
            out[acc] = {
                "account_id": acc,
                "scenario_id": index["account_to_scenario"][acc],
                "docs": [],
                "docs_rejected": [],
                "quarantined": [],
                "alarms": [{"kind": "dossier_build_failed", "account": acc, "error": repr(exc)}],
            }
    return out
