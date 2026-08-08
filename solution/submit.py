"""Снапшот отправки (раздел 3): любая отправленная попытка воспроизводима
байт в байт. out/submission.json -> out/submission-<N>.json, work/llm_cache
(ровно тот кэш, что лёг в этот submission) -> out/cache-<N>/, run-report
рядом под тем же номером. N — следующий свободный номер снапшота.

util.OUT/util.WORK читаются через модуль, а не биндятся именами при импорте:
тесты монкепатчат их для изоляции от реального out/work репозитория.
"""

import json
import shutil

import util

# Отпечаток публичного набора — из того же снимка, что читает стоп-проверка
# sanity.py. Общий источник осознан: живость обоих держится на одной
# предполётной строке ранбука («стоп-проверка жива»), и baseline с хешем от
# старой упаковки убил бы сразу обе — а проверять одну строку в окне дешевле,
# чем две.
_BASELINE = util.ROOT / "eval" / "public_baseline.json"


def _public_dataset_hash() -> str | None:
    try:
        return json.loads(_BASELINE.read_text())["dataset_hash"]
    except Exception:
        return None


def _refuse_if_public_run(out) -> None:
    """Снапшот публичного прогона не снимается (ревью PR #18, круг 3).

    Гейты Makefile закрывают вход, но публичный прогон — штатный путь: `make
    solve` и `make determinism` его прямо предполагают, и оба оставляют в
    out/submission.json результат по публичному набору. Следующий `make submit`
    снял бы его снапшотом как кандидата на отправку. Проверка на выходе — одна
    точка на все пути перезаписи разом, вместо гейта на каждую цель.

    Fail-open, если происхождение прогона не установлено: нет run-report, битый
    JSON, нет baseline. Отправленная работа лучше неотправленной, поэтому
    неизвестность печатается, но не блокирует, — блокирует только доказанный
    публичный отпечаток.
    """
    public = _public_dataset_hash()
    report = out / "run-report.json"
    try:
        got = json.loads(report.read_text())["dataset_hash"]
        # Отчёт старше submission — он от другого прогона, и судить по нему
        # нельзя ни в какую сторону (ревью PR #18, круг 4). Корень чинится в
        # solve.main, где отчёт снимается вместе с записью скелета; здесь —
        # страховка на случай, когда submission.json пришёл не оттуда
        # (восстановлен из снапшота, положен руками).
        if report.stat().st_mtime < (out / "submission.json").stat().st_mtime:
            got = None
    except Exception:
        got = None
    if got is None or public is None:
        print(
            "происхождение прогона не установлено (нет run-report или baseline) — снапшот снимается как есть",
            flush=True,
        )
        return
    if got == public:
        print(
            f"!!! ОТКАЗ: прогон принадлежит ПУБЛИЧНОМУ НАБОРУ (dataset_hash {got}) !!!\n"
            "  out/submission.json — ответы по публичному набору, снапшот кандидатом на отправку не будет.\n"
            "  Перезапустите боевой прогон: make run ARCHIVE=<приватный>.zip",
            flush=True,
        )
        raise SystemExit(1)


def _next_n(out) -> int:
    nums = []
    for p in out.glob("submission-*.json"):
        suffix = p.stem.removeprefix("submission-")
        if suffix.isdigit():
            nums.append(int(suffix))
    return max(nums, default=0) + 1


def _diff_answers(old: dict, new: dict) -> list[str]:
    """Ячейки, изменившиеся между прогонами (раздел 3: расхождение — в
    отчёт, а не молчаливая перезапись)."""
    changed = []
    for sc in sorted(set(old) | set(new)):
        old_cells, new_cells = old.get(sc, {}), new.get(sc, {})
        for cl in sorted(set(old_cells) | set(new_cells)):
            if old_cells.get(cl) != new_cells.get(cl):
                changed.append(f"{sc}.{cl}: {old_cells.get(cl)} -> {new_cells.get(cl)}")
    return changed


def snapshot() -> int:
    out = util.OUT
    _refuse_if_public_run(out)  # до любого копирования: отказ не оставляет половины снапшота
    n = _next_n(out)
    sub_dst = out / f"submission-{n}.json"
    shutil.copy2(out / "submission.json", sub_dst)
    print(f"submission: {sub_dst}")

    cache_src = util.WORK / "llm_cache"
    if cache_src.is_dir():
        cache_dst = out / f"cache-{n}"
        shutil.copytree(cache_src, cache_dst)
        print(f"cache: {cache_dst}")

    report_src = out / "run-report.json"
    if report_src.exists():
        report_dst = out / f"run-report-{n}.json"
        shutil.copy2(report_src, report_dst)
        print(f"run-report: {report_dst}")

    prev = out / f"submission-{n - 1}.json"
    if prev.exists():
        old = json.loads(prev.read_text()).get("answers", {})
        new = json.loads(sub_dst.read_text()).get("answers", {})
        diff = _diff_answers(old, new)
        print(f"diff vs submission-{n - 1}.json: {len(diff)} изменённых ячеек")
        for line in diff:
            print(f"  {line}")
    return n


def main() -> None:
    snapshot()


if __name__ == "__main__":
    main()
