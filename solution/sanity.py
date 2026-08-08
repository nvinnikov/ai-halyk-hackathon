"""Sanity-скрипт (раздел 6): без LLM, секунды. Диф против публичного снимка —
готовый список того, что сломается на новом наборе.
"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "solution")

from ledger import extract_archive, find_inputs, load_ledger, rows_of
from pdftext import extract_pages
from scindex import build_index
from util import workdir

BASELINE = Path("eval/public_baseline.json")

# Ключи, для которых расхождение с baseline — не сигнал, а гарантированное
# и ожидаемое положение вещей: dataset_hash разный у любого другого архива
# по определению; fallback_rate на настоящем приватном прогоне будет None
# (sanity зовётся первым, до solve.main, — трейсов extracted-прогона ещё
# нет), и печать этой строки как «что сломается» была бы шумом на каждом
# честном запуске по плану окна (sanity в 11:00, прогон в 11:10).
# stage_alarms — тот же довод, что и у fallback_rate: диагностика прошлого
# прогона на этом work/<hash>, а не свойство архива — сравнивать с
# публичным baseline бессмысленно (и на приватном наборе прогона может ещё
# не быть, sanity вызывается первой).
# public_score — ручной якорь скоринга публичного набора (не поле, которое
# считает collect()); диф с ним печатал бы вечный DIFF public_score: N -> None.
_DIFF_SKIP = {"dataset_hash", "fallback_rate", "stage_alarms", "public_score"}


def _fallback_rate(wd: Path) -> float | None:
    """Доля ячеек с tier > 0 из трейсов extracted-прогона; None, если трейсов
    ещё нет (sanity сам LLM не зовёт и extracted-прогон не запускает —
    только читает то, что уже лежит в work/<hash>/trace от прошлого
    прогона).

    Гейт по facts_source обязателен: в expected-режиме tier==0 всегда по
    построению (факты эталонные, фолбэков нет), и записанный оттуда
    потолок 0.0 сделал бы check_fallback_rate (задача 26) недостижимым на
    любом настоящем extracted-прогоне. Источник читается из
    <сценарий>.borrower.json — там facts_source пишется один раз на
    заёмщика (solve._write_borrower_trace)."""
    trace_dir = wd / "trace"
    if not trace_dir.is_dir():
        return None
    sources = {
        json.loads(p.read_text()).get("facts_source") for p in sorted(trace_dir.glob("*.borrower.json"))
    }
    if sources != {"extracted"}:
        return None
    cell_traces = []
    for p in sorted(trace_dir.glob("*.json")):
        if p.stem.endswith(".borrower"):
            continue  # <сценарий>.borrower.json — не пер-ячейковый трейс
        payload = json.loads(p.read_text())
        if "tier" in payload:
            cell_traces.append(payload)
    if not cell_traces:
        return None
    fell_back = sum(1 for t in cell_traces if t.get("tier", 0) > 0)
    return fell_back / len(cell_traces)


def _stage_alarms(wd: Path) -> dict[str, int] | None:
    """Число алярмов по видам в route/dossier/facts/specs — отравленный
    прогон виден до боевого запуска, без LLM (docs/ops/recovery-playbook.md).

    stages.artifact кэширует по версии стадии, не по успешности build(): если
    build() поймал ошибку модели (в первую очередь llm.SchemaRejected —
    исчерпанный баланс/схема) и вернула деградировавший, но валидный dict,
    этот dict ложится на диск как обычный успех и отдаётся повторно на любом
    следующем прогоне. None — ни одна из четырёх стадий ещё не строилась на
    этом work/<hash> (sanity сам их не строит); {} — стадии есть, алярмов
    нет."""
    dirs = [wd / sub for sub in ("route", "dossier", "facts", "specs")]
    if not any(d.is_dir() for d in dirs):
        return None
    counts: dict[str, int] = {}
    # Глобальный дедуп точных дублей (как в solve._alarm_counts, ревью PR #9,
    # 17-я волна): карантинные алярмы лежат И в route/*.json, И в каждом
    # dossier/*.json — пер-каталожный дедуп задваивал счётчик (266 при 133
    # реальных), а по нему принимают решения в окне.
    seen_exact: set[str] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            for alarm in json.loads(p.read_text()).get("alarms", []):
                kind = str(alarm.get("kind", "other")) if isinstance(alarm, dict) else "other"
                key = json.dumps(alarm, sort_keys=True, ensure_ascii=False)
                if key in seen_exact:
                    continue
                seen_exact.add(key)
                counts[kind] = counts.get(kind, 0) + 1
    return counts


def _doc_type_breakdown(wd: Path) -> dict[str, int] | None:
    """Разбивка документов по doc_type из route-артефактов прошлого прогона;
    None, если route ещё не строился (route/dossier не пишутся без полного
    прогона solve.main)."""
    route_dir = wd / "route"
    if not route_dir.is_dir():
        return None
    counts = Counter(
        json.loads(p.read_text()).get("doc_type", "unknown") for p in sorted(route_dir.glob("*.json"))
    )
    return dict(sorted(counts.items()))


def collect(archive: Path) -> dict:
    ds_hash, input_dir = extract_archive(archive)
    wd = workdir(ds_hash)
    inputs = find_inputs(input_dir)
    template = json.loads(inputs["template"].read_text())
    # Леджер строится в отдельном подкаталоге, а не в общем work/<hash>/ledger.json:
    # sanity обязан быть без LLM, то есть без второго яруса категоризации
    # (load_ledger без target_scenarios его и не запускает). Пиши sanity в общий
    # путь — solve.main запустился бы позже на том же архиве, увидел бы файл с
    # совпавшей версией стадии (stages.artifact — инвалидация по версии, не по
    # содержимому) и молча переиспользовал бы sanity-артефакт без LLM-категорий:
    # расход целевого заёмщика остался бы в OTHER и исказил бы EBITDA в боевом
    # прогоне. Ровно порядок окна 9 августа — sanity в 11:00, прогон в 11:10.
    art = load_ledger(wd / "sanity", input_dir)
    rows = rows_of(art)
    targets = sorted(template["answers"])
    index = build_index(rows, targets)
    target_accounts = set(index["scenario_to_account"].values())
    currencies = Counter(
        r["currency"] for r in rows if r["account_id"] in target_accounts and r["currency"] != "USD"
    )
    blind = 0
    for pdf in inputs["pdfs"]:
        blind += sum(1 for p in extract_pages(wd, pdf)["pages"] if p["blind"])
    clauses = sorted({cl for cells in template["answers"].values() for cl in cells})
    return {
        "dataset_hash": ds_hash,
        "targets": len(targets),
        # account_ids (сотни счетов) в снимок не тащим (ревью PR #9, 16-я
        # волна): DIFF на приватном наборе превращался бы в блоб, полезный
        # сигнал — счётчики accounts/rows/row_share.
        "background": {k: v for k, v in index["background"].items() if k != "account_ids"},
        "index_alarms": index["alarms"],
        "pdf_count": len(inputs["pdfs"]),
        "blind_pages": blind,
        "dirty_rows": len(art["dirty"]),
        "currencies_target": dict(sorted(currencies.items())),
        "clauses": clauses,
        "fallback_rate": _fallback_rate(wd),
        "stage_alarms": _stage_alarms(wd),
    }


def _resolve_fallback_rate(new_rate: float | None, base: dict | None) -> tuple[float | None, str | None]:
    """Что писать в fallback_rate при --write-baseline, и предупреждение,
    если есть.

    sanity — единственный писатель baseline. Новый None поверх уже известного
    числа в старом baseline — не первая генерация, а регресс данных
    (например, trace/ между --write-baseline перезаписался expected-режимом,
    задачи 25/26 отмечали эту коллизию): затирать проверенное число None
    молча нельзя — check_fallback_rate (задача 26) тихо перестал бы
    работать. Старое значение сохраняется, регресс — явным ALARM в stdout."""
    if new_rate is None and base is not None and base.get("fallback_rate") is not None:
        old = base["fallback_rate"]
        warning = (
            f"ALARM fallback_rate_write_regression: новый fallback_rate=None поверх "
            f"старого {old!r} — похоже на трейсы не от extracted-прогона; сохранено старое значение"
        )
        return old, warning
    return new_rate, None


def _resolve_public_score(base: dict | None) -> float | None:
    """Что писать в public_score при --write-baseline.

    collect() его не считает (public_score — внешний скоринг результата
    прогона, не sanity-метрика), поэтому в отличие от fallback_rate тут нет
    «нового» значения вообще — только перенос старого, чтобы якорь не пропадал
    молча при каждой перегенерации baseline."""
    return base.get("public_score") if base else None


def diff_baselines(got: dict, base: dict) -> list[str]:
    out = []
    for key in sorted((set(got) | set(base)) - _DIFF_SKIP):
        if got.get(key) != base.get(key):
            out.append(f"{key}: {base.get(key)!r} -> {got.get(key)!r}")
    return out


def main() -> int:
    archive = Path(sys.argv[1])
    s = collect(archive)
    print(f"dataset_hash: {s['dataset_hash']}")
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else None
    if base and s["dataset_hash"] == base["dataset_hash"]:
        print("!!! dataset_hash СОВПАЛ С ПУБЛИЧНЫМ НАБОРОМ — это не приватный архив !!!")
    for k, v in sorted(s.items()):
        print(f"{k}: {v}")
    doc_types = _doc_type_breakdown(workdir(s["dataset_hash"]))
    if doc_types is None:
        print("doc_types: unknown (прогона не было)")
    else:
        print(f"doc_types: {doc_types}")
    if "--write-baseline" in sys.argv:
        rate, warning = _resolve_fallback_rate(s["fallback_rate"], base)
        if warning:
            print(warning)
        s = {**s, "fallback_rate": rate, "public_score": _resolve_public_score(base)}
        BASELINE.write_text(json.dumps(s, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        print(f"baseline записан в {BASELINE}")
        return 0
    if base:
        for line in diff_baselines(s, base):
            print(f"DIFF {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
