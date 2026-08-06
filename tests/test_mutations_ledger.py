"""Мутация описаний леджера — holdout для пяти категорий, которого нет в данных.

Ключ не меняется: суммы, счета и даты не трогаются, поэтому ground_truth
остаётся верен байт в байт (5.1).
"""

import csv
import json
from pathlib import Path

import pytest
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


def test_mutation_counts_exact(tmp_path):
    """Точные числа замера: регрессия, потерявшая часть из 47 описаний или
    перекинувшая их между категориями (но сохранившая все пять непустыми),
    не должна проходить незамеченной — она делает следующий замер тише."""
    _, report = _mutated(tmp_path)
    assert report["by_category"] == {
        "CAPEX": 10,
        "CONSULTING": 4,
        "FINANCING": 2,
        "OTHER_OPEX": 15,
        "REVENUE": 16,
    }
    assert sum(report["by_category"].values()) == 47


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


GT = json.loads(Path("dataset/agentic-bank-public/ground_truth.json").read_text())["scenarios"]

# Восстановление, которого требуем от второго яруса, по худшей из трёх
# перестановок. REVENUE и OTHER_OPEX — жёстко и априори: обе входят в EBITDA,
# то есть в каждую коэффициентную ячейку, и частичное восстановление там
# означает систематический сдвиг всех коэффициентов. Остальные три —
# None до шага фиксации (значения впечатываются по факту первого замера).
FLOORS: dict[str, float | None] = {
    "REVENUE": 1.0,
    "OTHER_OPEX": 1.0,
    "CAPEX": None,
    "CONSULTING": None,
    "FINANCING": None,
}


@pytest.mark.llm
def test_second_tier_regression_on_sewer_levy():
    """Единственное описание набора, где второй ярус вызывается без мутации.

    Тест заведомо зелёный: разово это уже проверено. Его задача — чтобы
    правка промпта, схемы или таксономии не сломала ветку молча."""
    from categorize_llm import categorize_batch

    descs = sorted({d for d in _source_descriptions() if "sewer discharge levy" in d.lower()})
    assert descs, "описания Sewer discharge levy исчезли из набора"
    got, alarms = categorize_batch(descs)
    assert [a for a in alarms if a["kind"] != "category_rejected"] == []
    assert {got.get(d) for d in descs} == {"UTILITIES"}


@pytest.mark.llm
def test_recovery_by_category_worst_of_three_orders(tmp_path):
    """Точность восстановления по худшей из трёх перестановок (5.2.1)."""
    from categorize_llm import categorize_batch

    _, report = _mutated(tmp_path)
    origin = report["origin"]
    descs = sorted(origin)

    worst: dict[str, float] = {}
    for order in ("sorted", "reverse", "hash"):
        got, _ = categorize_batch(descs, order=order)
        by_cat: dict[str, list[bool]] = {}
        for d, want in sorted(origin.items()):
            by_cat.setdefault(want, []).append(got.get(d) == want)
        for cat, hits in by_cat.items():
            share = sum(hits) / len(hits)
            worst[cat] = min(worst.get(cat, 1.0), share)
        print(f"order={order}: " + ", ".join(f"{c}={sum(h) / len(h):.2f}" for c, h in sorted(by_cat.items())))

    print("ХУДШЕЕ ПО ТРЁМ: " + ", ".join(f"{c}={v:.2f}" for c, v in sorted(worst.items())))
    failed = {c: worst[c] for c, floor in FLOORS.items() if floor is not None and worst.get(c, 0.0) < floor}
    assert failed == {}, f"восстановление ниже требуемого: {failed}"


@pytest.mark.llm
def test_mutated_run_scores_and_is_deterministic(tmp_path):
    """Полный прогон на мутированном архиве против неизменного ключа."""
    import solve
    from score import score

    z = build_mutated_archive(tmp_path / "mutated.zip")
    a = solve.main(z, facts_source="expected")
    total = score(a, GT, verbose=True)
    print(f"СКОР НА МУТИРОВАННОМ АРХИВЕ: {total:.2f} (публичный потолок 34.00)")
    b = solve.main(z, facts_source="expected")
    assert a == b, "второй прогон разошёлся: детерминизм через кэш не держится"
