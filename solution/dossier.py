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

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pdftext import doc_hash
from route import borrower_name, full_text, route_doc, route_group_doc
from stages import artifact

# v6 — активационный бамп (2026-08-08): досье собиралось из route-артефактов
# v1; после ROUTE_VERSION=2 (META/WHOSE по тексту без футера) кэшированное
# досье устарело по входу. v5: алярмы карантина в артефакте досье.
DOSSIER_VERSION = 12
# v12 — ревью PR #23, вторая волна: сбой META второго прохода больше не
# деградирует досье, добавлена проверка издателя отчётности.
# v11 — ревью PR #23: второй проход не запускается при деградации первого;
# набор документов в досье от этого зависит.
# v10 — документы группового уровня, привязанные по наименованию заёмщика
# (route.route_group_doc), приходят в досье отдельной областью видимости
# (scope="group"): набор документов на входе фактов изменился.
# v7 — кумулятивные типы больше не отдают замененные редакции: рабочий
# документ с маркером «заменён окончательным отчётом» вносил в досье
# предварительную реклассификацию операции и искажал две ячейки заёмщика.
# v8/v9 — то же правило распространено на черновики типов с окончательной
# формой (DRAFT_NOT_EFFECTIVE_TYPES): промежуточные ведомости аудитора несли по
# одной предварительной реклассификации каждая, и обе искажали по ячейке.
_EDITION_RANK = {"final": 0, "unmarked": 1, "draft": 2, "superseded": 3}
# Редакционная фильтрация — только для перевыпускаемых целиком типов. Отчёты
# и записки кумулятивны: каждый несёт своё документальное решение, и отброс
# по дате терял бы реклассификации и исправления сумм (правка по замеру).
CUMULATIVE_TYPES = frozenset({"audit_report", "treasury_memo", "financial_notes"})
# Типы, у которых черновик не несёт решения. Аудиторский отчёт выпускается в
# окончательной форме, а промежуточная ведомость прямо пишет, что «полностью
# заменяется отчётом о выполнении согласованных процедур», что руководствоваться
# следует исключительно окончательным отчётом и что первоначальная
# классификация сохраняется, — то есть сама себя отменяет, и её маркер модель с
# равным правом читает как draft и как superseded.
#
# Записка казначейства — рабочий документ по своей природе, окончательной формы
# у неё нет и заменённой она себя не объявляет: её черновик несёт настоящее
# исправление суммы, не выгруженной в реестр, и отбрасывать его нельзя (замер:
# отброс сразу стоил ячейки).
#
# Это приближение настоящего признака — «документ объявляет себя заменённым».
# Настоящий признак виден только в тексте, и место ему в route, где текст читает
# модель; enum редакции его сейчас не выражает.
DRAFT_NOT_EFFECTIVE_TYPES = frozenset({"audit_report"})


def _is_effective(doc: dict, dtype: str) -> bool:
    """Несёт ли документ кумулятивного типа действующее решение."""
    if doc["edition"] == "superseded":
        return False
    return not (doc["edition"] == "draft" and dtype in DRAFT_NOT_EFFECTIVE_TYPES)


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


def _all_current(wd: Path, targets: list[str]) -> bool:
    """Все ли досье на диске собраны ТЕКУЩЕЙ версией стадии.

    Существования файла мало: artifact() сверяет ещё и версию, и при её росте
    пересоберёт досье — а значит второй проход всё-таки нужен.
    """
    for acc in targets:
        path = wd / "dossier" / f"{acc}.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return False
        if data.get("_meta", {}).get("stage_version") != DOSSIER_VERSION:
            return False
    return True


def _attach_by_name(
    wd: Path,
    targets: list[str],
    routed: list[dict],
    quarantined: list[dict],
    by_hash: dict[str, Path],
    first_pass_degraded: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Документы группового уровня, привязанные по наименованию заёмщика.

    Наименования собираются из уже отмаршрутизированных документов счёта, то
    есть из того, что первый проход про этот счёт уже установил по номеру.
    Счёт без единого привязанного документа наименования не даёт — и это
    правильно: брать его было бы неоткуда, кроме как из самого кандидата.
    """
    if targets and _all_current(wd, targets):
        # Все досье уже лежат на диске в текущей версии — artifact() их и так
        # вернёт готовыми, а проход успел бы прочитать полный текст сотни с
        # лишним документов впустую (ревью PR #23, вторая волна). В окне это
        # прямая цена рестарта, а рестарт — штатный сценарий ранбука.
        return [], []
    if first_pass_degraded:
        # Второй проход целиком построен на результатах первого: наименование
        # считается по документам, которые первый проход привязал, а решение по
        # кандидату — по пулу этих наименований. При деградации первого прохода
        # и то и другое неполно, но кэшируется как успех: ни набор документов,
        # ни пул в ключи артефактов borrower/route_group не входят, а
        # stages.artifact инвалидируется только по версии. Пустое наименование
        # и посчитанный при неполном пуле отказ пережили бы устранение причины
        # (ревью PR #23, замечание 1). Прохода при деградации нет вовсе — досье
        # и так помечено деградированным и на диск не ляжет.
        return [], [{"kind": "name_pass_skipped_degraded_routing"}]
    unrouted = [
        q
        for q in sorted(quarantined, key=lambda x: x["file"])
        if q.get("quarantine_reason") == "no_account_mentions" and q.get("doc_hash") in by_hash
    ]
    if not unrouted:
        return [], []
    alarms: list[dict] = []
    names: list[tuple[str, str]] = []
    for acc in targets:
        mine = sorted((r for r in routed if r["account_id"] == acc), key=lambda d: d["file"])
        if not mine:
            continue
        try:
            found = borrower_name(wd, acc, [by_hash[r["doc_hash"]] for r in mine if r["doc_hash"] in by_hash])
        except Exception as exc:
            print(f"ALARM borrower_name_failed {acc}: {exc!r}", flush=True)
            alarms.append({"kind": "borrower_name_failed", "account": acc, "error": repr(exc)})
            continue
        alarms.extend(found["alarms"])
        if found["name"]:
            names.append((acc, found["name"]))
    if any(a["kind"] == "borrower_name_failed" for a in alarms):
        # Пул наименований неполон по транзиентной причине (бюджет, сеть, промах
        # кассеты), а артефакт route_group кэшируется по хешу документа и пул в
        # ключ не входит: посчитанный сейчас отказ пережил бы устранение причины
        # и молча отменил бы привязку на всех следующих прогонах. Прохода
        # сегодня нет — досье и так помечено деградированным и не закрепится.
        return [], alarms
    if not names:
        return [], alarms

    def route_or_skip(q: dict) -> dict | None:
        path = by_hash[q["doc_hash"]]
        try:
            return route_group_doc(wd, path, sorted(names))
        except Exception as exc:
            # Тот же рубеж, что у _route_or_quarantine: сбой одного документа
            # не имеет права уронить список остальных. Документ просто остаётся
            # в карантине, как и был до прохода.
            print(f"ALARM group_routing_failed {path.name}: {exc!r}", flush=True)
            alarms.append({"kind": "group_routing_failed", "file": path.name, "error": repr(exc)})
            return None

    workers = int(os.environ.get("SOLVE_WORKERS", "4"))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(route_or_skip, unrouted))
    attached = []
    for r in results:
        if r is None:
            continue
        # Сбой META ВТОРОГО прохода переименовывается намеренно (ревью PR #23,
        # вторая волна). Второй проход зовёт META по каждому непривязанному
        # документу — на публичном наборе их 122, на приватном будет свой
        # хвост мусора, и один SchemaRejected среди них практически неизбежен.
        # Под общим именем он ставил бы degraded=True и запрещал запись ВСЕХ
        # двенадцати досье — то есть отменял бы рестарт в окне из-за документа,
        # который и без того остаётся в карантине. Свой route_group-артефакт
        # при этом всё равно не кэшируется, поэтому следующий прогон по нему
        # перепытается; цена компромисса — досье может закрепиться без
        # документа, который на повторе привязался бы, и это видно алярмом.
        alarms.extend(
            {**a, "kind": "group_meta_extraction_failed"} if a.get("kind") == "meta_extraction_failed" else a
            for a in r["alarms"]
        )
        if not r["quarantined"]:
            attached.append(r)
    return attached, alarms


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

    # Деградация ПЕРВОГО прохода считается до второго: второй построен на его
    # результатах целиком, и запускать его по неполному `routed` нельзя.
    _ROUTING_DEGRADED = {"routing_failed", "meta_extraction_failed"}
    routing_degraded = any(
        a.get("kind") in _ROUTING_DEGRADED for q in quarantined for a in q.get("alarms", [])
    )

    # Второй проход по наименованию заёмщика — только по документам, которые
    # первый проход не привязал НИ К ЧЕМУ (`no_account_mentions`). Фоновый
    # документ и документ нерелевантного типа уже решены и не переигрываются:
    # там счёт напечатан, и наименование его не отменяет.
    attached, name_alarms = _attach_by_name(
        wd, targets, routed, quarantined, by_hash, first_pass_degraded=routing_degraded
    )
    if attached:
        by_group_hash = {g["doc_hash"]: g for g in attached}
        quarantined = [q for q in quarantined if q["doc_hash"] not in by_group_hash]

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
    # borrower_name_failed / group_routing_failed — та же природа: транзиентный
    # сбой второго прохода означает, что досье собрано без документов
    # группового уровня, и закреплять такое на диске нельзя.
    _degraded_kinds = _ROUTING_DEGRADED | {"borrower_name_failed", "group_routing_failed"}
    degraded = any(
        a.get("kind") in _degraded_kinds
        for a in [*(a for q in quarantined for a in q.get("alarms", [])), *name_alarms]
    )

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
                    # недействующей редакции не бывает действующим — даже
                    # единственный (см. _is_effective).
                    ordered_type = sorted(by_type[dtype], key=lambda d: (d["date"], d["file"]))
                    actives = [d for d in ordered_type if _is_effective(d, dtype)]
                    kept = actives[0]["file"] if actives else None
                    rej = [
                        {"file": d["file"], "reason": f"{d['edition']}_edition", "kept": kept}
                        for d in ordered_type
                        if not _is_effective(d, dtype)
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
                            "scope": "borrower",
                            "text": full_text(wd, by_hash[active["doc_hash"]]),
                        }
                    )
                docs_rejected.extend(rej)
            # Документы группового уровня — отдельной областью видимости и МИМО
            # выбора действующей редакции по типу: они относятся к материнской
            # компании, и редакции документов заёмщика того же типа ими не
            # перебиваются (иначе консолидированная отчётность вытеснила бы
            # аудиторский отчёт по самому заёмщику). Недействующая редакция
            # отбрасывается тем же правилом, что и везде.
            for g in sorted((g for g in attached if g["account_id"] == acc), key=lambda d: d["file"]):
                if not _is_effective(g, g["doc_type"]):
                    docs_rejected.append(
                        {"file": g["file"], "reason": f"{g['edition']}_edition", "kept": None}
                    )
                    continue
                docs.append(
                    {
                        "file": g["file"],
                        "doc_type": g["doc_type"],
                        "date": g["date"],
                        "scope": "group",
                        "text": full_text(wd, by_hash[g["doc_hash"]]),
                    }
                )
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
                    [*(a for q in quarantined for a in q.get("alarms", [])), *name_alarms],
                    key=lambda a: (a.get("file", ""), a.get("account", ""), a.get("kind", "")),
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
