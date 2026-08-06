"""Сшивка досье: маршрутизированные документы группируются по целевым счетам,
среди редакций одного типа остаётся действующая (5.2.1).

Документы адресуются doc_hash, а не именем файла: базовые имена во вложенных
каталогах приватного архива могут коллидировать. Маршрутизация независима по
файлам и идёт пулом потоков (SOLVE_WORKERS, дефолт 4) — ограничитель здесь
rate limit LLM; результаты собираются в детерминированном порядке.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pdftext import doc_hash
from route import full_text, route_doc
from stages import artifact

DOSSIER_VERSION = 1
_EDITION_RANK = {"final": 0, "unmarked": 1, "draft": 2, "superseded": 3}


def _neg_date(date: str) -> str:
    d = date or "0000-00-00"
    return "".join(chr(0xFFFF - ord(c)) for c in d)  # сортировка по убыванию даты


def _pick_active(docs: list[dict]) -> tuple[dict, list[dict]]:
    """Маркер перебивает дату: сначала ранг редакции, затем дата по убыванию."""
    ranked = sorted(docs, key=lambda d: (_EDITION_RANK[d["edition"]], _neg_date(d["date"]), d["file"]))
    active, rejected = ranked[0], []
    for d in ranked[1:]:
        reason = (
            "edition_marker"
            if _EDITION_RANK[d["edition"]] != _EDITION_RANK[active["edition"]]
            else "superseded_by_date"
        )
        rejected.append({"file": d["file"], "reason": reason, "kept": active["file"]})
    return active, rejected


def build_dossiers(
    wd: Path, pdfs: list[Path], index: dict, all_accounts: list[str] | None = None
) -> dict[str, dict]:
    """all_accounts — все счета леджера: по ним route отличает фоновый документ
    от документа без счетов вовсе (карантин без алярма против карантина с ним)."""
    targets = sorted(index["account_to_scenario"])
    ordered = sorted(pdfs, key=lambda x: (x.name, str(x)))
    workers = int(os.environ.get("SOLVE_WORKERS", "4"))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda p: route_doc(wd, p, targets, all_accounts), ordered))
    routed = [r for r in results if not r["quarantined"]]
    quarantined = [r for r in results if r["quarantined"]]
    by_hash = {doc_hash(p): p for p in ordered}

    out: dict[str, dict] = {}
    for acc in targets:

        def build(acc=acc) -> dict:
            mine = [r for r in routed if r["account_id"] == acc]
            docs, docs_rejected = [], []
            by_type: dict[str, list[dict]] = {}
            for r in mine:
                by_type.setdefault(r["doc_type"], []).append(r)
            for dtype in sorted(by_type):
                active, rej = _pick_active(by_type[dtype])
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
            }

        out[acc] = artifact(wd / "dossier" / f"{acc}.json", DOSSIER_VERSION, build)
    return out
