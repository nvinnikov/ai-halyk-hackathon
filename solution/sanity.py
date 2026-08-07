"""Sanity-скрипт (раздел 6 спеки): без LLM, секунды. Диф против публичного
снимка — готовый список того, что сломается на новом наборе.

Запускается первым делом 9 августа, до пайплайна, поэтому обязан быть дешёвым
и не иметь права испортить работу самого пайплайна.
"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "solution")

from ledger import extract_archive, find_inputs, load_ledger, rows_of
from pdftext import blindness_criteria, extract_pages
from scindex import build_index
from util import ROOT, workdir

BASELINE = ROOT / "eval" / "public_baseline.json"

# Ключи, по которым диф не строится: они обязаны отличаться между наборами.
_DIFF_SKIP = {"dataset_hash", "fallback_rate", "doc_types"}


def _doc_types(wd: Path) -> dict | str:
    """Разбивка документов по типам добирается из route-артефактов прошлого
    прогона этого же архива. Прогона не было — так и говорим: звать ради этой
    строчки LLM нельзя, sanity обязан оставаться бесплатным и мгновенным."""
    files = sorted((wd / "route").glob("*.json"))
    if not files:
        return "unknown (прогона не было)"
    counter: Counter[str] = Counter()
    for f in files:
        counter[json.loads(f.read_text()).get("doc_type", "unknown")] += 1
    return dict(sorted(counter.items()))


def _fallback_rate(wd: Path) -> float | None:
    """Доля ячеек, посчитанных не ярусом dsl. Потолок для check_fallback_rate
    (задача 26) берётся отсюда, поэтому считается только по трейсам настоящего
    extracted-прогона: нет трейсов или прогон был эталонный — None, и проверка
    честно пропускается вместо сравнения с выдуманным числом.

    Гейт по facts_source обязателен. На эталонном прогоне (facts_source
    "expected") фолбэков нет по построению, и записанный с него потолок 0.0
    инвариант 26 сделал бы недостижимым: любая ячейка, ушедшая на лестницу в
    настоящем прогоне, валила бы проверку."""
    trace_dir = wd / "trace"
    sources = {
        json.loads(f.read_text()).get("facts_source") for f in sorted(trace_dir.glob("*.borrower.json"))
    }
    if sources != {"extracted"}:
        return None
    cells = [
        json.loads(f.read_text())
        for f in sorted(trace_dir.glob("*.*.json"))
        if not f.name.endswith(".borrower.json")
    ]
    cells = [t for t in cells if "tier" in t]
    if not cells:
        return None
    return round(sum(1 for t in cells if t.get("tier", 0) > 0) / len(cells), 4)


def collect(archive: Path) -> dict:
    ds_hash, input_dir = extract_archive(archive)
    wd = workdir(ds_hash)
    inputs = find_inputs(input_dir)
    template = json.loads(inputs["template"].read_text())
    targets = sorted(template["answers"])

    # Леджер строится в отдельном каталоге, а не в общем work/<hash>/ledger.json.
    # Sanity обязан быть без LLM, то есть без второго яруса категоризации; запиши
    # он общий артефакт — solve переиспользовал бы его по совпадению версии стадии
    # и молча потерял бы LLM-категории: расход остался бы в OTHER и завысил EBITDA.
    art = load_ledger(wd / "sanity", input_dir)
    rows = rows_of(art)

    index = build_index(rows, targets)
    target_accounts = set(index["scenario_to_account"].values())
    currencies = Counter(
        r["currency"] for r in rows if r["account_id"] in target_accounts and r["currency"] != "USD"
    )

    # Пограничные страницы считаются раздельно по направлению, и это не
    # педантизм: на публичном наборе «мало символов, но числа есть» — 1 страница,
    # а «текста много, чисел мало» — 159 (титулы и оглавления). Сложить их в одно
    # число значило бы утопить сигнал про сканы в стабильном шуме. Рост первого
    # счётчика на приватном наборе — повод переключить правило слепоты на «ИЛИ»
    # прямо в окне.
    blind = 0
    short_with_numbers = 0
    long_with_few_numbers = 0
    for pdf in inputs["pdfs"]:
        for page in extract_pages(wd, pdf)["pages"]:
            few_chars, few_numbers = blindness_criteria(page["text"])
            blind += int(few_chars and few_numbers)
            short_with_numbers += int(few_chars and not few_numbers)
            long_with_few_numbers += int(few_numbers and not few_chars)

    clauses = sorted({cl for cells in template["answers"].values() for cl in cells})
    return {
        "dataset_hash": ds_hash,
        "targets": len(targets),
        "background": index["background"],
        "index_alarms": index["alarms"],
        "ledger_alarms": art["alarms"],
        "pdf_count": len(inputs["pdfs"]),
        "blind_pages": blind,
        "blind_borderline": {
            "short_with_numbers": short_with_numbers,
            "long_with_few_numbers": long_with_few_numbers,
        },
        "dirty_rows": len(art["dirty"]),
        "currencies_target": dict(sorted(currencies.items())),
        "clauses": clauses,
        "doc_types": _doc_types(wd),
        "fallback_rate": _fallback_rate(wd),
    }


def diff_baselines(got: dict, base: dict) -> list[str]:
    """Каждая строка дифа — это «что сломается»."""
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
    if "--write-baseline" in sys.argv:
        BASELINE.write_text(json.dumps(s, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        print(f"baseline записан в {BASELINE}")
        return 0
    if base is None:
        print("baseline отсутствует — сравнивать не с чем")
        return 0
    diff = diff_baselines(s, base)
    for line in diff:
        print(f"DIFF {line}")
    if not diff:
        print("DIFF нет: структура набора совпадает с публичным")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
