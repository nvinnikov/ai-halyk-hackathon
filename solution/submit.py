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
