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

Замена обязана быть нейтральной: новое описание не содержит имени своей
категории и его очевидных синонимов. Первая редакция словаря это правило
нарушала — 'sales settlement' переписывалось в 'revenue recognised...',
'Purchase of' в 'Capital acquisition of', — и замер отвечал на вопрос
«узнаёт ли модель категорию, названную в тексте» вместо «обобщает ли она
на незнакомую формулировку». Показательно, что единственная нейтральная
замена той редакции ('equipment' -> 'machinery') и вскрыла настоящий
дефект промпта, тогда как остальные проходили без единого промаха.
Инвариант проверяется тестом test_replacements_are_neutral.
"""

import csv
import shutil
from pathlib import Path

from categorize import categorize
from public_archive import DATASET as PUBLIC_DATASET
from public_archive import pack_dataset

MUTATED_CATEGORIES = frozenset({"REVENUE", "CAPEX", "OTHER_OPEX", "CONSULTING", "FINANCING"})

# Слова, выдающие категорию: их присутствие в новом описании превращает замер
# в проверку словаря вместо проверки обобщения. 'tranche' и 'credit' — потому
# что промпт описывает FINANCING как «кредитные транши», 'capital' — потому
# что CAPEX и есть capital expenditure.
CATEGORY_GIVEAWAYS = frozenset(
    {"revenue", "sales", "capital", "capex", "consulting", "financing", "credit", "tranche"}
)

# Порядок значим: длинные фразы раньше коротких.
REPLACEMENTS: list[tuple[str, str]] = [
    # 'legal' сохраняется намеренно: это существо операции, а не триггер
    # правила. Замена обязана снимать формулировку, по которой срабатывает
    # регулярка, но не смысл строки — иначе замер меряет угадывание.
    ("dispute arbitration and legal servicing", "legal support for dispute resolution"),
    ("servicing and operating costs", "upkeep and running costs"),
    ("operating and maintenance expenses", "running and upkeep expenses"),
    ("cleaning and clearance works", "desilting works"),
    ("servicing contract", "upkeep contract"),
    ("remediation", "restoration"),
    ("servicing", "upkeep"),
    ("sales settlement", "customer remittance against issued invoice"),
    ("Purchase of", "Acquisition of"),
    ("equipment", "machinery"),
    ("facility drawdown", "proceeds received"),
    ("Advisory engagement on", "Expert review of"),
    ("Management advisory retainer", "Standing expert support for management"),
    ("Management retainer fee", "Fee for standing expert support"),
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
    src = PUBLIC_DATASET
    work = Path(dst_zip).parent / (Path(dst_zip).stem + "_src")
    mutate_ledger(src, work)
    return pack_dataset(work / src.name, Path(dst_zip))
