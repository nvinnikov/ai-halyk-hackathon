"""Мутация описаний леджера — holdout для пяти категорий, которого нет в данных.

Ключ не меняется: суммы, счета и даты не трогаются, поэтому ground_truth
остаётся верен байт в байт (5.1).
"""

import csv
import json
import os
import re
import shutil
from pathlib import Path

import pytest
from mutations_ledger import (
    CATEGORY_GIVEAWAYS,
    MUTATED_CATEGORIES,
    build_mutated_archive,
    mutate_description,
    mutate_ledger,
)

from categorize import categorize
from ledger import find_inputs
from public_archive import pack_dataset
from templates import TEMPLATES

DATASET = Path("dataset/agentic-bank-public")
# Публичный архив: имя от организаторов (см. tools/public_archive.py). Общая
# константа для сравнения байтов мутированного архива и для чистого прогона,
# перезаписывающего out/submission.json после llm-тестов (см. finally ниже).
PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")


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


def test_replacements_are_neutral(tmp_path):
    """Второй инвариант: новое описание не называет свою категорию.

    Ослеплённая регулярка ещё не значит честный замер. Если синоним содержит
    имя категории ('sales settlement' -> 'revenue recognised...'), тест меряет
    словарь, а не обобщение, и снятый с него порог завышенно оптимистичен."""
    _, report = _mutated(tmp_path)
    leaks = {
        d: sorted(w for w in CATEGORY_GIVEAWAYS if w in d.lower())
        for d in report["origin"]
        if any(w in d.lower() for w in CATEGORY_GIVEAWAYS)
    }
    assert leaks == {}, f"имя категории просочилось в описание: {leaks}"


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
    фильтра, а не из-за категоризации, и замер покажет ложную деградацию.

    Список токенов вычитывается из TEMPLATES, а не зашивается константой:
    задача 24 собирает cellspec из извлечённых спек, и новый desc_contains
    появится там раньше, чем кто-нибудь вспомнит про этот тест. Токен под
    существующую замену (`equipment`, `servicing`, `remediation` уже в
    REPLACEMENTS) дал бы ровно ту ложную деградацию, ради которой тест и
    заведён."""
    tokens = set(re.findall(r"desc_contains\('([^']*)'\)", " ".join(TEMPLATES.values())))
    if not tokens:
        # Единственный desc_contains ушёл из библиотеки вместе с английской
        # иглой 'subsidiary' (языковой хардкод под публичный леджер); тест
        # остаётся гвардом на случай возвращения фильтра в шаблоны.
        pytest.skip("в TEMPLATES нет desc_contains — мутациям нечего сохранять")
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
    assert z.read_bytes() != PUBLIC_ZIP.read_bytes()


def test_archive_bytes_do_not_depend_on_mtime(tmp_path):
    """Одинаковое содержимое — одинаковые байты, а значит один dataset_hash.

    zf.write() брал бы время из mtime файла: mutate_ledger переписывает CSV
    каждый вызов, а git clone ставит mtime всем файлам в момент клонирования.
    Архив плыл бы и между прогонами, и между чекаутами, заводя новый work/ на
    каждый — мутированный прогон никогда не был бы тёплым."""
    src = tmp_path / "src" / DATASET.name
    shutil.copytree(DATASET, src)
    first = pack_dataset(src, tmp_path / "a.zip").read_bytes()
    for p in sorted(src.rglob("*")):
        if p.is_file():
            st = p.stat()
            os.utime(p, (st.st_atime, st.st_mtime + 86_400))
    second = pack_dataset(src, tmp_path / "b.zip").read_bytes()
    assert first == second, "байты архива зависят от mtime — dataset_hash поплывёт"


GT = json.loads(Path("dataset/agentic-bank-public/ground_truth.json").read_text())["scenarios"]

# Восстановление, которого требуем от второго яруса, по худшей из трёх
# перестановок. REVENUE и OTHER_OPEX — жёстко и априори: обе входят в EBITDA,
# то есть в каждую коэффициентную ячейку, и частичное восстановление там
# означает систематический сдвиг всех коэффициентов. Остальные три измерены
# (см. ниже); незафиксированный порог ловит test_all_floors_are_measured.
#
# Замер 2026-08-08, LLM_PROVIDER=gemini, gemini-3.6-flash: худшее по трём
# перестановкам — 1.00 по всем пяти категориям. Команда замера:
#   LLM_PROVIDER=gemini GEMINI_API_KEY=... uv run pytest \
#       tests/test_mutations_ledger.py -m llm -q -s -k recovery
#
# ВАЖНО О ПРОИСХОЖДЕНИИ ПОРОГОВ. Дефолтный провайдер — anthropic
# (llm.py: LLM_PROVIDER по умолчанию), боевая модель — claude-haiku-4-5. На
# этом пути пороги НЕ снимались: приведённые значения измерены на Gemini.
# Числа модельно-специфичны, поэтому падение теста на anthropic-прогоне
# означает «на haiku восстановление хуже, чем на gemini», а не обязательно
# дефект промпта — прежде чем править промпт, сверьтесь, каким провайдером
# идёт прогон. Пересъём на боевой модели — одна команда с ANTHROPIC_API_KEY.
FLOORS: dict[str, float | None] = {
    "REVENUE": 1.0,
    "OTHER_OPEX": 1.0,
    "CAPEX": 1.0,
    "CONSULTING": 1.0,
    "FINANCING": 1.0,
}


def test_all_floors_are_measured():
    """Незафиксированный порог обязан быть виден без API.

    Проверка внутри llm-теста ловит забытый шаг только у того, кто запускает
    живой прогон, а он помечен `llm` и в `make check` с CI не попадает — там
    на второй ярус не смотрело бы ничего, и «не измерено» выглядело бы как
    «прошло». Тип сохраняет `| None` намеренно: состояние «порог ещё не
    снят» законно между заменой модели и пересъёмом, но обязано падать
    здесь, а не тихо выпадать из условия `if floor is not None` в
    llm-тесте восстановления по трём перестановкам."""
    unmeasured = sorted(c for c, floor in FLOORS.items() if floor is None)
    assert unmeasured == [], (
        f"пороги не измерены: {unmeasured}. Снимаются живым прогоном — "
        "LLM_PROVIDER=<провайдер> ...API_KEY=... uv run pytest "
        "tests/test_mutations_ledger.py -m llm -q -s -k recovery, "
        "значения из строки «ХУДШЕЕ ПО ТРЁМ», округлённые вниз до сотых. "
        "До фиксации эти категории не проверяет ни один гейт."
    )


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
    failed = {
        c: worst.get(c, 0.0) for c, floor in FLOORS.items() if floor is not None and worst.get(c, 0.0) < floor
    }
    assert failed == {}, f"восстановление ниже требуемого: {failed}"


@pytest.mark.llm
def test_mutated_run_scores_and_is_deterministic(tmp_path):
    """Полный прогон на мутированном архиве против неизменного ключа.

    solve.main всегда пишет out/submission.json, независимо от архива —
    без finally на диске остался бы submission мутированного прогона,
    внешне неотличимый от настоящего, а это единственный артефакт, который
    уходит на проверку (см. tests/test_solution.py::
    test_other_unassigned_written_when_rows_lost — тот же приём)."""
    import solve
    from score import score

    try:
        z = build_mutated_archive(tmp_path / "mutated.zip")
        a = solve.main(z, facts_source="expected")
        total = score(a, GT, verbose=True)
        print(f"СКОР НА МУТИРОВАННОМ АРХИВЕ: {total:.2f} (публичный потолок 34.00)")
        b = solve.main(z, facts_source="expected")
        assert a == b, "второй прогон разошёлся: детерминизм через кэш не держится"
    finally:
        solve.main(PUBLIC_ZIP, facts_source="expected")
