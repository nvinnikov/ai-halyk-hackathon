"""Снапшот отправки (раздел 3): любая отправленная попытка воспроизводима
байт в байт. out/submission.json -> out/submission-<N>.json, work/llm_cache
(ровно тот кэш, что лёг в этот submission) -> out/cache-<N>/, run-report
рядом под тем же номером. N — следующий свободный номер снапшота.

util.OUT/util.WORK читаются через модуль, а не биндятся именами при импорте:
тесты монкепатчат их для изоляции от реального out/work репозитория.
"""

import json
import os
import shutil

import util


def _run_was_public(out) -> bool | None:
    """Прогон, оставивший этот submission, шёл по публичному набору?

    None — установить не удалось: нет отчёта, битый JSON, отчёт от другого
    прогона, поле не записано (отчёт от версии до этой правки).

    Вердикт читается ГОТОВЫМ из run-report: его пишет solve, где архив под
    рукой, сравнением байтов леджера. Выводить его здесь из хранимого
    отпечатка нельзя (ревью PR #18, круг 5) — eval/public_baseline.json не
    константа, `sanity.py <любой>.zip --write-baseline` кладёт туда хеш
    переданного архива.
    """
    report = out / "run-report.json"
    try:
        # Отчёт старше submission — он от другого прогона, и судить по нему
        # нельзя ни в какую сторону (ревью PR #18, круг 4). Корень чинится в
        # solve.main, где отчёт снимается вместе с записью скелета; здесь —
        # страховка на случай, когда submission.json пришёл не оттуда
        # (восстановлен из снапшота, положен руками).
        if report.stat().st_mtime < (out / "submission.json").stat().st_mtime:
            return None
        value = json.loads(report.read_text()).get("is_public_dataset")
    except Exception:
        return None
    return value if isinstance(value, bool) else None


def _refuse_if_public_run(out) -> None:
    """Снапшот публичного прогона не снимается (ревью PR #18, круг 3).

    Гейты Makefile закрывают вход, но публичный прогон — штатный путь: `make
    solve` и `make determinism` его прямо предполагают, и оба оставляют в
    out/submission.json результат по публичному набору. Следующий `make submit`
    снял бы его снапшотом как кандидата на отправку. Проверка на выходе — одна
    точка на все пути перезаписи разом, вместо гейта на каждую цель.

    Fail-open везде, где происхождение установить не удалось: неизвестность
    печатается, но не блокирует. Блокирует только доказанный публичный прогон —
    там снапшот и правда снимать нечего.

    У блокировки есть обход SUBMIT_FORCE=1, и он несущий (ревью PR #18,
    круг 7): вердикт под ней — эвристика, `_is_public_dataset` сравнивает
    только байты леджера. Приватный пакет, приехавший с тем же
    master_ledger_2025.csv и другими документами, опознался бы как публичный
    (гейт require-private-archive такое не поймает — зипы разные), и отказ без
    обхода стал бы тупиком: совет перезапустить прогон даёт тот же вердикт.
    """
    was_public = _run_was_public(out)
    if was_public is None:
        print(
            "происхождение прогона не установлено (нет run-report или он от другого прогона) — "
            "снапшот снимается как есть",
            flush=True,
        )
        return
    if was_public:
        if os.environ.get("SUBMIT_FORCE") == "1":
            print(
                "SUBMIT_FORCE=1: прогон опознан как публичный, снапшот снимается принудительно",
                flush=True,
            )
            return
        print(
            "!!! ОТКАЗ: прогон шёл по ПУБЛИЧНОМУ НАБОРУ !!!\n"
            "  out/submission.json — ответы по публичному набору, снапшот кандидатом на отправку не будет.\n"
            "  Перезапустите боевой прогон: make run ARCHIVE=<приватный>.zip\n"
            "  Если это ЛОЖНОЕ срабатывание (приватный набор с тем же леджером) — "
            "SUBMIT_FORCE=1 make submit",
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
