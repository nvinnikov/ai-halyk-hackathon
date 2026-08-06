"""Мутация описаний леджера: holdout для пяти категорий, которого нет в данных.

Фоновые счета дают честный holdout для восьми категорий (640 невиданных
описаний, правила ловят 629). Для REVENUE, CAPEX, OTHER_OPEX, CONSULTING и
FINANCING фон пуст — генератор не выдаёт фоновым счетам ни выручки, ни
капзатрат, — а именно на них висит EBITDA. Эта мутация создаёт недостающую
проверку: описания переписываются синонимами, суммы и счета не трогаются,
ключ остаётся верен байт в байт.

Замены — подстрочные и упорядоченные: длинные фразы раньше коротких, иначе
'servicing' съест 'servicing contract'. Список ревьюируется глазами, а его
корректность проверяется кодом (tests/test_mutations_ledger.py).
"""

import csv
import shutil
from pathlib import Path

from categorize import categorize
from public_archive import pack_dataset

MUTATED_CATEGORIES = frozenset({"REVENUE", "CAPEX", "OTHER_OPEX", "CONSULTING", "FINANCING"})

# Порядок значим: длинные фразы раньше коротких.
REPLACEMENTS: list[tuple[str, str]] = [
    ("dispute arbitration and legal servicing", "dispute resolution support"),
    ("servicing and operating costs", "upkeep and running costs"),
    ("operating and maintenance expenses", "running and upkeep expenses"),
    ("cleaning and clearance works", "desilting works"),
    ("servicing contract", "upkeep contract"),
    ("remediation", "restoration"),
    ("servicing", "upkeep"),
    ("sales settlement", "revenue recognised on customer contracts"),
    ("Purchase of", "Capital acquisition of"),
    ("equipment", "machinery"),
    ("facility drawdown", "credit line disbursement"),
    ("Advisory engagement on", "Consulting mandate for"),
    ("Management advisory retainer", "Executive consulting arrangement"),
    ("Management retainer fee", "Executive consulting charge"),
]


def mutate_description(text: str) -> str:
    """Синоним вместо триггерной фразы. Описания вне пятёрки не задеваются —
    их триггеры в списке замен отсутствуют."""
    if categorize(text) not in MUTATED_CATEGORIES:
        return text
    out = text
    for old, new in REPLACEMENTS:
        out = out.replace(old, new)
    return out


def mutate_ledger(src_root: Path, dst_root: Path) -> dict:
    """Копия датасета с переписанными описаниями. Возвращает отчёт замера."""
    src_root = Path(src_root)
    dst = Path(dst_root) / src_root.name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src_root, dst)

    csvs = sorted(dst.glob("*.csv"))
    assert len(csvs) == 1, f"ожидался один CSV в корне датасета, найдено {len(csvs)}"
    path = csvs[0]

    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames or [])
        rows = list(reader)

    origin: dict[str, str] = {}
    by_category: dict[str, int] = {}
    mutated = 0
    for r in rows:
        old = r["description"]
        cat = categorize(old)
        new = mutate_description(old)
        if new == old:
            continue
        r["description"] = new
        mutated += 1
        if new not in origin:
            origin[new] = cat
            by_category[cat] = by_category.get(cat, 0) + 1

    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    return {
        "rows_mutated": mutated,
        "by_category": dict(sorted(by_category.items())),
        "origin": dict(sorted(origin.items())),
    }


def build_mutated_archive(dst_zip: Path) -> Path:
    """Архив мутированного датасета. Упаковывается тем же pack_dataset, что и
    публичный: иначе разные байты дали бы расходящиеся dataset_hash при
    одинаковом содержимом."""
    src = Path("dataset/agentic-bank-public")
    work = Path(dst_zip).parent / (Path(dst_zip).stem + "_src")
    mutate_ledger(src, work)
    return pack_dataset(work / src.name, Path(dst_zip))
