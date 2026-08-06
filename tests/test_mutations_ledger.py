"""Мутация описаний леджера — holdout для пяти категорий, которого нет в данных.

Ключ не меняется: суммы, счета и даты не трогаются, поэтому ground_truth
остаётся верен байт в байт (5.1).
"""

import csv
from pathlib import Path

from mutations_ledger import (
    MUTATED_CATEGORIES,
    build_mutated_archive,
    mutate_description,
    mutate_ledger,
)

from categorize import categorize
from ledger import find_inputs
from templates import TEMPLATES

DATASET = Path("dataset/agentic-bank-public")


def _mutated(tmp_path) -> tuple[Path, dict]:
    dst = tmp_path / "mutated"
    report = mutate_ledger(DATASET, dst)
    return dst, report


def test_every_mutated_description_blinds_the_rules(tmp_path):
    """Главный инвариант: мутация обязана ослепить первый ярус целиком.

    Описание, всё ещё ловящееся правилом, делает замер бессмысленным —
    тест, не проверяющий обещанного, хуже отсутствующего."""
    _, report = _mutated(tmp_path)
    survivors = {d: categorize(d) for d in report["origin"] if categorize(d) != "OTHER"}
    assert survivors == {}, f"правила всё ещё ловят: {survivors}"


def test_all_five_categories_covered(tmp_path):
    """Мутируются ровно пять категорий без holdout'а, и каждая непуста."""
    _, report = _mutated(tmp_path)
    assert set(report["by_category"]) == set(MUTATED_CATEGORIES)
    assert all(n > 0 for n in report["by_category"].values())


def test_only_description_changes(tmp_path):
    """Суммы, счета, даты и валюты не тронуты — иначе поедет ключ."""
    dst, _ = _mutated(tmp_path)
    src_rows = list(csv.DictReader(open(find_inputs(DATASET.parent)["ledger_csv"], newline="")))
    dst_rows = list(csv.DictReader(open(find_inputs(dst)["ledger_csv"], newline="")))
    assert len(src_rows) == len(dst_rows)
    for a, b in zip(src_rows, dst_rows, strict=True):
        for key in a:
            if key != "description":
                assert a[key] == b[key], f"{key} изменился в {a['txn_id']}"


def test_desc_contains_tokens_survive(tmp_path):
    """Токены фильтров desc_contains сохраняются: иначе ячейка упадёт из-за
    фильтра, а не из-за категоризации, и замер покажет ложную деградацию."""
    tokens = {"subsidiary"}
    assert any("desc_contains" in t for t in TEMPLATES.values())
    _, report = _mutated(tmp_path)
    for token in tokens:
        src = [d for d in _source_descriptions() if token in d.lower()]
        assert src, f"токен {token} не встречается в исходных описаниях"
        for d in src:
            assert token in mutate_description(d).lower()


def _source_descriptions() -> list[str]:
    path = find_inputs(DATASET.parent)["ledger_csv"]
    return [r["description"] for r in csv.DictReader(open(path, newline=""))]


def test_untouched_categories_are_untouched(tmp_path):
    """Восемь проверенных категорий мутация не задевает."""
    for d in _source_descriptions():
        if categorize(d) not in MUTATED_CATEGORIES:
            assert mutate_description(d) == d, f"задето лишнее: {d!r}"


def test_ground_truth_unchanged(tmp_path):
    """Ключ копируется байт в байт: это условие честности всего замера."""
    dst, _ = _mutated(tmp_path)
    src_gt = (DATASET / "ground_truth.json").read_bytes()
    dst_gt = (dst / DATASET.name / "ground_truth.json").read_bytes()
    assert src_gt == dst_gt


def test_mutation_is_deterministic(tmp_path):
    """Две сборки подряд дают идентичный CSV."""
    a, _ = _mutated(tmp_path / "a")
    b, _ = _mutated(tmp_path / "b")
    assert find_inputs(a)["ledger_csv"].read_bytes() == find_inputs(b)["ledger_csv"].read_bytes()


def test_archive_builds_and_differs(tmp_path):
    """Архив собирается и отличается от публичного — значит другой
    dataset_hash и отдельный каталог work/."""
    z = build_mutated_archive(tmp_path / "mutated.zip")
    assert z.exists() and z.stat().st_size > 0
    assert z.read_bytes() != Path("6a741640c31eb032062683.zip").read_bytes()
