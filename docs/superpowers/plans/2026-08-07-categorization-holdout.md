# Категоризация: holdout, мутация описаний и поячеечный алярм — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть слепую зону категоризации леджера: сделать потерю строки громкой (поячеечный алярм) и создать недостающий holdout для пяти категорий, у которых его нет в данных (мутация описаний).

**Architecture:** Алярм считается из уже собираемых данных — категории из AST метрики и строки заёмщика после `prepare_rows`; логика живёт в `taxonomy.py`, `solve.py` только вызывает её в блоке диагностики рядом с `sign_divergence`. Мутация — подстрочные замены триггерных фраз в `description` целевых строк; корректность мутации проверяется кодом (`categorize(new) == "OTHER"`), а не глазами. Ключ не меняется, потому что суммы, счета и даты не трогаются.

**Tech Stack:** Python 3.12 (uv), стандартная библиотека. Новых зависимостей нет.

**Спека:** `docs/superpowers/specs/2026-08-06-categorization-holdout-design.md`. Ссылки вида «5.2» ниже — на её разделы.

## Global Constraints

- Код, идентификаторы, логи — на английском; комментарии, докстринги, сообщения коммитов — на русском.
- **Греп-гейт (раздел 9 основной спеки):** ни одного имени заёмщика, номера пункта (`6.1`), порогового числа, префикса `TXN-`/`ACC-` в `solution/` — только в `tests/` и `eval/`. Словарь замен и списки описаний живут в `eval/`, не в `solution/`.
- **Детерминизм:** нигде `random()`, `time.time()` в логике, итерации по несортированным `set`/`dict`. Всё сортировать перед использованием. Деньги — только `Decimal`.
- `null` в `actual` не существует как состояние. Каждая ячейка обёрнута fail-open индивидуально; **диагностика не имеет права стоить ячейки** — любой новый вызов в `solve.py` обёрнут `try/except`, как соседний `sign_divergence`.
- **Гейт 34.00 обязан остаться зелёным.** `BASELINE` в `tests/test_solution.py` не понижается ни при каких обстоятельствах. Любая правка, поднимающая публичный скор выше 34.00, — подгонка под ключ и отвергается (34.00 — доказанный потолок набора).
- `make check` (ruff format + ruff check + mypy + pytest) зелёный перед каждым коммитом. Новые файлы прогонять через `uv run ruff format .`.
- Тесты, ходящие в Anthropic API, помечаются `@pytest.mark.llm` и не входят в `make check`.
- Запуск тестов: `uv run pytest tests/test_<x>.py -q` из корня (conftest выставляет cwd и sys.path).
- **Алярм не меняет вердикт** (5.3). Ячейка считается как считалась; алярм только пишется в трейс и печатается.

## Карта файлов

```
solution/taxonomy.py          + cell_other_alarm(), поправка докстроки про OTHER ⊂ ALL
solution/categorize.py        + докстрока про ортогональность категории и знака
solution/categorize_llm.py    + параметр order для замера разброса (дефолт не меняется)
solution/solve.py             + _all_metric_categories(), вызов алярма в блоке диагностики
eval/mutations_ledger.py      новый: словарь замен, mutate_ledger(), инварианты
tests/test_taxonomy.py        + тесты cell_other_alarm
tests/test_mutations_ledger.py новый: инварианты мутации (без API) + llm-тесты
```

## Порядок задач

1. **Task 1** — `cell_other_alarm` в `taxonomy.py` + докстроки. Не жертвуется ни при каких условиях: работает на любом наборе.
2. **Task 2** — вызов алярма в `solve.py`. Завершает пункт 5.3 спеки.
3. **Task 3** — мутация и её инварианты (без API).
4. **Task 4** — llm-замеры: регрессия второго яруса и полный прогон на мутированном архиве.

Порядок жертв при отставании (5.3 и раздел 7 спеки): Task 4 откладывается первой, Task 3 без неё бессмысленна и уходит следом, Task 1–2 не жертвуются.

---

### Task 1: `cell_other_alarm` и поправки докстрок

**Files:**
- Modify: `solution/taxonomy.py` (докстрока модуля, новая функция в конец файла)
- Modify: `solution/categorize.py` (докстрока модуля)
- Test: `tests/test_taxonomy.py`

**Interfaces:**
- Consumes: `taxonomy.expand(name) -> frozenset[str]`, `taxonomy.LEAVES`, `taxonomy.ROLLUPS` (существуют).
- Produces: `taxonomy.cell_other_alarm(rows: list[dict], referenced: set[str]) -> dict | None`.
  Строка `rows` — словарь с ключами `txn_id: str`, `cat: str`, `amt: Decimal`.
  Возврат `None` означает «алярма нет». Непустой возврат:
  ```python
  {
      "blind": ["CAPEX", "REVENUE"],     # отсортированный список слепых категорий
      "other_sum": "18255335.65",        # str(Decimal), сумма |amt| строк в OTHER
      "inputs_sum": "2098450950.32",     # str(Decimal), сумма |amt| строк в blind-листьях
      "severity": "0.008699",            # str(Decimal), 6 знаков; None если inputs_sum == 0
      "txn_ids": ["..."],                # отсортированные txn_id строк в OTHER
  }
  ```

- [ ] **Step 1: Написать падающие тесты**

В `tests/test_taxonomy.py` дополнить существующий импорт taxonomy до
`from taxonomy import LEAVES, ROLLUPS, cell_other_alarm, coverage_report, expand, is_category`
(`Decimal` там уже импортирован) и дописать в конец файла:

```python
def _row(txn: str, cat: str, amt: str) -> dict:
    return {"txn_id": txn, "cat": cat, "amt": Decimal(amt)}


def test_no_alarm_when_other_empty():
    """Нет неразнесённых строк — нет и алярма."""
    rows = [_row("T-1", "REVENUE", "100"), _row("T-2", "CAPEX", "-50")]
    assert cell_other_alarm(rows, {"REVENUE"}) is None


def test_no_alarm_when_metric_reads_all():
    """ALL включает OTHER: неразнесённые строки метрика и так считает."""
    rows = [_row("T-1", "REVENUE", "100"), _row("T-2", "OTHER", "-40")]
    assert cell_other_alarm(rows, {"ALL"}) is None


def test_alarm_when_blind_category_and_other_present():
    """Метрика читает REVENUE, часть суммы осела в OTHER — потеря молчаливая."""
    rows = [_row("T-1", "REVENUE", "100"), _row("T-2", "OTHER", "-25")]
    a = cell_other_alarm(rows, {"REVENUE"})
    assert a is not None
    assert a["blind"] == ["REVENUE"]
    assert a["other_sum"] == "25"
    assert a["inputs_sum"] == "100"
    assert a["severity"] == "0.250000"
    assert a["txn_ids"] == ["T-2"]


def test_rollup_expanded_to_leaves():
    """Роллап разворачивается: OPEX_TOTAL слеп к OTHER так же, как его листья."""
    rows = [_row("T-1", "PAYROLL", "-80"), _row("T-2", "RENT", "-20"), _row("T-3", "OTHER", "-10")]
    a = cell_other_alarm(rows, {"OPEX_TOTAL"})
    assert a is not None
    assert a["inputs_sum"] == "100"  # PAYROLL + RENT, оба листья OPEX_TOTAL


def test_severity_none_when_metric_inputs_empty():
    """Метрика читает категорию, где строк нет вовсе: severity не считается,
    но алярм есть — это максимальная тяжесть, а не её отсутствие."""
    rows = [_row("T-1", "OTHER", "-10")]
    a = cell_other_alarm(rows, {"CAPEX"})
    assert a is not None
    assert a["severity"] is None
    assert a["inputs_sum"] == "0"


def test_unknown_category_treated_as_blind():
    """Незнакомая категория считается слепой: fail-open не должен молчать."""
    rows = [_row("T-1", "OTHER", "-10"), _row("T-2", "REVENUE", "50")]
    a = cell_other_alarm(rows, {"NOT_A_CATEGORY"})
    assert a is not None
    assert a["blind"] == ["NOT_A_CATEGORY"]


def test_no_alarm_without_referenced():
    """Категории метрики неизвестны — судить не о чем."""
    assert cell_other_alarm([_row("T-1", "OTHER", "-10")], set()) is None


def test_deterministic_output():
    """Порядок blind и txn_ids не зависит от порядка входа."""
    rows = [_row("T-9", "OTHER", "-1"), _row("T-1", "OTHER", "-2"), _row("T-5", "REVENUE", "10")]
    a = cell_other_alarm(rows, {"REVENUE", "CAPEX"})
    b = cell_other_alarm(list(reversed(rows)), {"CAPEX", "REVENUE"})
    assert a == b
    assert a["blind"] == ["CAPEX", "REVENUE"]
    assert a["txn_ids"] == ["T-1", "T-9"]
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_taxonomy.py -q`
Expected: FAIL — `ImportError: cannot import name 'cell_other_alarm' from 'taxonomy'`

- [ ] **Step 3: Реализовать функцию**

Дописать в конец `solution/taxonomy.py`:

```python
def cell_other_alarm(rows: list[dict], referenced: set[str]) -> dict | None:
    """Потерянная строка глазами одной ячейки (5.3): что метрика не увидит.

    Слепа та категория, чьё развёртывание не содержит OTHER. Метрика,
    читающая ALL, неразнесённые строки считает — для неё алярма нет.

    Тяжесть меряется долей не от леджера заёмщика, а от того, что метрика
    вообще видит: 18 млн в OTHER при EBITDA 2.3 млн — катастрофа, при
    выручке 500 млн — шум. Порога у severity нет: алярм срабатывает при
    любой ненулевой сумме, severity задаёт лишь порядок разбора.
    """
    if not referenced:
        return None
    blind = []
    for name in sorted(referenced):
        try:
            leaves = expand(name)
        except KeyError:
            # Незнакомая категория (например, пришедшая от LLM) считается
            # слепой: молчать здесь опаснее, чем лишний раз предупредить.
            blind.append(name)
            continue
        if "OTHER" not in leaves:
            blind.append(name)
    if not blind:
        return None

    ordered = sorted(rows, key=lambda r: r["txn_id"])
    other_rows = [r for r in ordered if r["cat"] == "OTHER"]
    other_sum = sum((abs(r["amt"]) for r in other_rows), Decimal(0))
    if other_sum == 0:
        return None

    blind_leaves: set[str] = set()
    for name in blind:
        try:
            blind_leaves |= set(expand(name))
        except KeyError:
            continue
    inputs_sum = sum((abs(r["amt"]) for r in ordered if r["cat"] in blind_leaves), Decimal(0))
    severity = str((other_sum / inputs_sum).quantize(Decimal("0.000001"))) if inputs_sum else None
    return {
        "blind": blind,
        "other_sum": str(other_sum),
        "inputs_sum": str(inputs_sum),
        "severity": severity,
        "txn_ids": [r["txn_id"] for r in other_rows],
    }
```

- [ ] **Step 4: Запустить — проходит**

Run: `uv run pytest tests/test_taxonomy.py -q`
Expected: PASS, все тесты файла

- [ ] **Step 5: Поправить докстроку `taxonomy.py`**

Заменить докстроку модуля (строки 1–5) на:

```python
"""Двухуровневая таксономия категорий (5.5): листья и явные роллапы.

OTHER — корзина неразнесённого. В прикладные роллапы (OPEX_TOTAL) он не
входит: любая сумма в нём означает, что часть расхода потерялась и тихо
завышает EBITDA. Исключение — ALL, который по смыслу «все строки» и OTHER
содержит; поэтому agg(ALL, ...) неразнесённые строки считает, и метрики
связанных сторон промахом категоризации не задеты (см. cell_other_alarm).
"""
```

- [ ] **Step 6: Поправить докстроку `categorize.py`**

Заменить докстроку модуля (строки 1–5) на:

```python
"""Категоризация транзакций по назначению платежа.

Контрагент систематически не соответствует сути операции (Foxridge Stationery
платит налог на прибыль), поэтому классифицируем строго по description.

Категория отвечает на вопрос «о чём операция», знак — на вопрос «приход или
расход». Измерения ортогональны и разведены параметром sign в DSL, поэтому
доходные строки в расходной на вид категории — не баг: проценты, полученные
по эскроу-остатку, — это по-прежнему проценты, и agg(INTEREST, out) их не
возьмёт. Переносить их в отдельную категорию значило бы смешать два
измерения обратно в одно.
"""
```

- [ ] **Step 7: Прогнать всё и закоммитить**

Run: `uv run ruff format . && make check`
Expected: PASS, 34.00 не изменилось

```bash
git add solution/taxonomy.py solution/categorize.py tests/test_taxonomy.py
git commit -m "feat: поячеечный алярм неразнесённых строк и ортогональность категории и знака"
```

---

### Task 2: Вызов алярма в solve

**Files:**
- Modify: `solution/solve.py` (новая функция рядом с `_metric_categories:118`, вызов в блоке диагностики `main:400-408`)
- Test: `tests/test_solution.py`

**Interfaces:**
- Consumes: `taxonomy.cell_other_alarm` (Task 1).
- Produces: `solve._all_metric_categories(node) -> set[str]` — категории всех `Agg`-узлов **без фильтра по знаку**; ключ `other_unassigned` в трейсе ячейки.

**Почему не `_metric_categories`:** существующая функция отбрасывает узлы со `sign == "in"`, а потерянная строка `REVENUE` — главный риск (спека, раздел 3). Нужен обход без фильтра, как в `_write_borrower_trace:290`.

- [ ] **Step 1: Написать падающий тест**

Дописать в `tests/test_solution.py`:

```python
def _cell_traces() -> list[Path]:
    """Трейсы ячеек публичного прогона. Каталог адресуется отпечатком архива,
    иначе сюда попали бы трейсы мутированного прогона (задача 4)."""
    from util import dataset_hash, workdir

    return sorted((workdir(dataset_hash(PUBLIC_ZIP)) / "trace").glob("*.*.json"))


def test_other_unassigned_absent_on_public_set(answers):
    """На публичном наборе OTHER пуст у всех целевых — алярма быть не должно.

    Тест держит границу: срабатывание здесь означает, что категоризация
    поехала, а не что алярм неверен."""
    traces = _cell_traces()
    assert traces, "трейсы ячеек не найдены — прогон не состоялся"
    with_alarm = [
        t.name for t in traces if json.loads(t.read_text()).get("other_unassigned") is not None
    ]
    assert with_alarm == [], f"неожиданный other_unassigned: {with_alarm}"


def test_other_unassigned_written_when_rows_lost(monkeypatch):
    """Строка, ушедшая в OTHER, обязана поднять алярм в трейсе ячейки."""
    original = solve.load_rows

    def lossy(scenario, all_rows, index, facts, donor_rates):
        raw, rows, alarms = original(scenario, all_rows, index, facts, donor_rates)
        # Первая строка выручки «не опозналась»: ровно тот промах, ради
        # которого алярм и вводится.
        for r in rows:
            if r["cat"] == "REVENUE":
                r["cat"] = "OTHER"
                break
        return raw, rows, alarms

    monkeypatch.setattr(solve, "load_rows", lossy)
    try:
        solve.main(PUBLIC_ZIP, facts_source="expected")
        hit = [
            a
            for a in (json.loads(t.read_text()).get("other_unassigned") for t in _cell_traces())
            if a is not None
        ]
        assert hit, "потерянная строка REVENUE не подняла ни одного алярма"
        assert all(a["other_sum"] != "0" for a in hit)
    finally:
        # Трейсы на диске общие: испорченный прогон обязан быть переписан
        # чистым, иначе соседний тест увидит чужой алярм.
        monkeypatch.undo()
        solve.main(PUBLIC_ZIP, facts_source="expected")
```

Замечания исполнителю: `PUBLIC_ZIP`, `json`, `Path` и `solve` в этом файле уже импортированы — новых импортов на уровне модуля не добавлять. Фикстура `answers` — `scope="module"`, поэтому прогон она делает один раз; `_cell_traces` читает результат с диска.

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_solution.py -q -k other_unassigned`
Expected: FAIL — `test_other_unassigned_written_when_rows_lost` не находит алярмов (ключа в трейсе нет)

- [ ] **Step 3: Добавить обход категорий**

В `solution/solve.py` после `_metric_categories` (строка 122) добавить:

```python
def _all_metric_categories(node) -> set[str]:
    """Все категории метрики, включая доходные: потерянная строка REVENUE —
    главный риск категоризации, а _metric_categories отбрасывает sign == in."""
    return {n.category for n in walk(node) if isinstance(n, Agg)}
```

- [ ] **Step 4: Вызвать алярм в блоке диагностики**

В `solution/solve.py` в `main`, внутри `if isinstance(cellspec_or_error, dict):`, сразу после блока `sign_divergence` (после строки `trace["sign_divergence_error"] = repr(exc)`) добавить:

```python
                    # Неразнесённые строки глазами этой ячейки (5.3): диагностика,
                    # вердикт не меняется. Падение обхода не стоит ячейки.
                    try:
                        oa = cell_other_alarm(
                            rows, _all_metric_categories(cellspec_or_error["metric_ast"])
                        )
                        if oa is not None:
                            trace["other_unassigned"] = oa
                            print(
                                f"ALARM other_unassigned {scenario} {clause}: "
                                f"blind={','.join(oa['blind'])} severity={oa['severity']}",
                                flush=True,
                            )
                    except Exception as exc:
                        trace["other_unassigned_error"] = repr(exc)
```

И дополнить импорт на строке 34:

```python
from taxonomy import cell_other_alarm, coverage_report
```

- [ ] **Step 5: Запустить — проходит**

Run: `uv run pytest tests/test_solution.py -q`
Expected: PASS, включая `test_score_not_below_baseline` (34.00) и `test_deterministic`

- [ ] **Step 6: Закоммитить**

Run: `uv run ruff format . && make check`

```bash
git add solution/solve.py tests/test_solution.py
git commit -m "feat: алярм other_unassigned в трейсе ячейки"
```

---

### Task 3: Мутация описаний леджера

**Files:**
- Create: `eval/mutations_ledger.py`
- Modify: `tools/public_archive.py` (вынести упаковку в отдельную функцию)
- Test: `tests/test_mutations_ledger.py`

**Interfaces:**
- Consumes: `categorize.categorize(description) -> str`, `ledger.find_inputs(input_dir) -> dict` (ключи `root`, `template`, `ledger_csv`, `pdfs`), `templates.TEMPLATES`.
- Produces (в `tools/public_archive.py`): `pack_dataset(dataset_dir: Path, dst_zip: Path) -> Path` — упаковка каталога датасета в zip; `build_public_archive` становится её обёрткой.
- Produces:
  - `mutations_ledger.REPLACEMENTS: list[tuple[str, str]]` — упорядоченный список подстрочных замен;
  - `mutations_ledger.MUTATED_CATEGORIES: frozenset[str]` — пять категорий без holdout'а;
  - `mutations_ledger.mutate_description(text: str) -> str`;
  - `mutations_ledger.mutate_ledger(src_root: Path, dst_root: Path) -> dict` — отчёт:
    ```python
    {
        "rows_mutated": 141,
        "by_category": {"CAPEX": 10, "CONSULTING": 4, ...},   # уникальных описаний
        "origin": {"<новое описание>": "CAPEX", ...},          # новое → исходная категория
    }
    ```
  - `mutations_ledger.build_mutated_archive(dst_zip: Path) -> Path` — собирает архив из мутированного датасета.

**Данные:** пять категорий без holdout'а дают ровно 47 уникальных описаний у целевых заёмщиков, и все они строятся из четырнадцати триггерных фраз. Замены подобраны так, что после них `categorize` возвращает `OTHER` для всех 47, слово `subsidiary` сохраняется (его читает `desc_contains` в шаблоне `unrestricted_transfer_share`), и ни одна строка вне пятёрки не задета — это проверено на публичном наборе и закреплено тестами шага 1.

- [ ] **Step 1: Написать падающие тесты**

`tests/test_mutations_ledger.py`:

```python
"""Мутация описаний леджера — holdout для пяти категорий, которого нет в данных.

Ключ не меняется: суммы, счета и даты не трогаются, поэтому ground_truth
остаётся верен байт в байт (5.1).
"""

import csv
import json
from pathlib import Path

import pytest

from categorize import categorize
from ledger import find_inputs
from mutations_ledger import (
    MUTATED_CATEGORIES,
    build_mutated_archive,
    mutate_description,
    mutate_ledger,
)
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
    assert (
        find_inputs(a)["ledger_csv"].read_bytes()
        == find_inputs(b)["ledger_csv"].read_bytes()
    )


def test_archive_builds_and_differs(tmp_path):
    """Архив собирается и отличается от публичного — значит другой
    dataset_hash и отдельный каталог work/."""
    z = build_mutated_archive(tmp_path / "mutated.zip")
    assert z.exists() and z.stat().st_size > 0
    assert z.read_bytes() != Path("6a741640c31eb032062683.zip").read_bytes()
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_mutations_ledger.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mutations_ledger'`

- [ ] **Step 3: Вынести упаковку архива в общую функцию**

Спека 5.1 требует, чтобы мутированный архив паковался тем же кодом, что публичный: разные
способы упаковки дают разные байты, а значит расходящиеся `dataset_hash`. Сейчас упаковка
зашита внутрь `build_public_archive` вместе с путями. В `tools/public_archive.py` заменить
тело на:

```python
def pack_dataset(dataset_dir: Path, dst_zip: Path) -> Path:
    """Упаковать каталог датасета в zip. Верхний уровень внутри архива —
    имя каталога, как в оригинальной раздаче; порядок записей отсортирован,
    поэтому две сборки подряд дают один и тот же файл.

    Единственная реализация упаковки на всех потребителей: публичный архив,
    мутированный архив (eval/mutations_ledger.py), CI и conftest."""
    dataset_dir = Path(dataset_dir)
    dst_zip = Path(dst_zip)
    assert dataset_dir.is_dir(), f"нет каталога датасета: {dataset_dir}"
    tmp = dst_zip.with_name(dst_zip.name + ".tmp")
    dst_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(dataset_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(dataset_dir.parent))
    tmp.replace(dst_zip)
    return dst_zip


def build_public_archive(force: bool = False) -> Path:
    """Собрать публичный архив, если его нет."""
    if ARCHIVE.exists() and not force:
        return ARCHIVE
    return pack_dataset(DATASET, ARCHIVE)
```

Проверить, что публичный архив пересобирается байт в байт:

```bash
uv run python -c "
from pathlib import Path
import sys; sys.path.insert(0, 'tools')
from public_archive import ARCHIVE, build_public_archive
before = ARCHIVE.read_bytes()
build_public_archive(force=True)
assert ARCHIVE.read_bytes() == before, 'упаковка изменилась — поедет dataset_hash'
print('байты совпали')
"
```

Expected: `байты совпали`

- [ ] **Step 4: Реализовать модуль**

`eval/mutations_ledger.py`:

```python
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
```

- [ ] **Step 5: Запустить — проходит**

Run: `uv run pytest tests/test_mutations_ledger.py -q`
Expected: PASS. Если `test_every_mutated_description_blinds_the_rules` покажет выживших — добавить замену в `REPLACEMENTS` строго для их триггерной фразы, не трогая остальные.

- [ ] **Step 6: Проверить, что публичный гейт не сдвинулся**

Run: `make check`
Expected: PASS, `test_score_not_below_baseline` по-прежнему 34.00 (мутация живёт в `eval/` и обычного прогона не касается)

- [ ] **Step 7: Закоммитить**

```bash
uv run ruff format . && make check
git add eval/mutations_ledger.py tools/public_archive.py tests/test_mutations_ledger.py
git commit -m "feat: мутация описаний леджера как holdout пяти категорий"
```

---

### Task 4: Замер разброса и порогов (llm)

**Files:**
- Modify: `solution/categorize_llm.py` (параметр `order`, дефолт не меняется)
- Modify: `tests/test_mutations_ledger.py` (llm-тесты)
- Test: `tests/test_categorize_llm.py` (тест на новый параметр, без API)

**Interfaces:**
- Consumes: `mutations_ledger.build_mutated_archive`, `mutations_ledger.mutate_ledger` (Task 3); `solve.main(archive, facts_source="expected")`; `score.score(answers, gt_scenarios, verbose)`.
- Produces: `categorize_llm.categorize_batch(descriptions, order="sorted") -> tuple[dict[str, str], list[dict]]`; допустимые значения `order` — `"sorted"`, `"reverse"`, `"hash"`.

**Зачем параметр `order`:** кэш фиксирует первый пришедший ответ навсегда, поэтому один прогон измеряет один образец, а не распределение (5.2.1). Другой порядок описаний → другой текст промпта → другой ключ кэша → живой вызов при той же семантике. Три фиксированные перестановки дают три независимых замера, воспроизводимых через неделю. Рабочий путь остаётся на `"sorted"` и делает по-прежнему один вызов на пачку — это измерительный инструмент в тесте, а не self-consistency в ответе.

- [ ] **Step 1: Написать падающий тест на параметр (без API)**

Дописать в `tests/test_categorize_llm.py`:

```python
def test_order_changes_prompt_not_semantics(monkeypatch):
    """Три перестановки дают три разных промпта при одном наборе описаний."""
    import categorize_llm

    seen = []

    def fake_call(prompt, schema, version, **kw):
        seen.append(prompt)
        return {"categories": [{"description": d, "category": "UTILITIES"} for d in descs]}

    descs = ["Zebra levy", "Alpha levy", "Mango levy"]
    monkeypatch.setattr(categorize_llm.llm, "call", fake_call)
    for order in ("sorted", "reverse", "hash"):
        categorize_llm.categorize_batch(descs, order=order)
    assert len(set(seen)) == 3, "перестановки обязаны давать разные промпты"


def test_order_default_is_sorted(monkeypatch):
    """Дефолт рабочего пути не меняется."""
    import categorize_llm

    seen = []

    def fake_call(prompt, schema, version, **kw):
        seen.append(prompt)
        return {"categories": []}

    monkeypatch.setattr(categorize_llm.llm, "call", fake_call)
    categorize_llm.categorize_batch(["B item", "A item"])
    categorize_llm.categorize_batch(["B item", "A item"], order="sorted")
    assert seen[0] == seen[1]


def test_unknown_order_rejected():
    import pytest as _pytest

    import categorize_llm

    with _pytest.raises(ValueError):
        categorize_llm.categorize_batch(["x"], order="random")
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_categorize_llm.py -q -k order`
Expected: FAIL — `categorize_batch() got an unexpected keyword argument 'order'`

- [ ] **Step 3: Добавить параметр**

В `solution/categorize_llm.py` добавить импорт `import hashlib` и функцию перед `categorize_batch`:

```python
def _ordered(descriptions: list[str], order: str) -> list[str]:
    """Порядок описаний в пачке. Рабочий путь всегда sorted; остальные два
    нужны замеру разброса (спека 5.2.1): другой порядок даёт другой промпт,
    другой ключ кэша и, значит, независимый ответ модели при той же задаче."""
    unique = sorted(set(descriptions))
    if order == "sorted":
        return unique
    if order == "reverse":
        return list(reversed(unique))
    if order == "hash":
        return sorted(unique, key=lambda d: hashlib.sha256(d.encode()).hexdigest())
    raise ValueError(f"unknown order {order!r}")
```

Заменить сигнатуру и строку с `unique`:

```python
def categorize_batch(descriptions: list[str], order: str = "sorted") -> tuple[dict[str, str], list[dict]]:
```

```python
    unique = _ordered(descriptions, order)
```

Дописать в докстроку `categorize_batch` строку про параметр:

```
        order: порядок описаний в пачке — "sorted" (рабочий путь), "reverse"
            или "hash" (замер разброса, спека 5.2.1)
```

- [ ] **Step 4: Запустить — проходит**

Run: `uv run pytest tests/test_categorize_llm.py -q`
Expected: PASS

- [ ] **Step 5: Написать llm-тесты замера**

Дописать в `tests/test_mutations_ledger.py`:

```python
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
```

- [ ] **Step 6: Прогнать замер с API**

Run: `ANTHROPIC_API_KEY=... uv run pytest tests/test_mutations_ledger.py -m llm -q -s`
Expected: печатаются доли по трём перестановкам, худшее по категориям и скор.

Разбор результата:
- `REVENUE` или `OTHER_OPEX` ниже 1.0 → **решение не годится**, независимо от общего скора. Разбираться через печать промаха: какое описание и в какую категорию ушло; чинить промпт `CAT_PROMPT`, а не порог.
- `CAPEX`, `CONSULTING`, `FINANCING` → записать фактическое худшее в `FLOORS` вместо `None`, округлив вниз до сотых.
- Скор ниже 34.00 → нормально и ожидаемо; цифра идёт в отчёт, порогом не становится (общий скор здесь вторичен, точность по категориям информативнее).

- [ ] **Step 7: Зафиксировать измеренные пороги**

Заменить `None` в `FLOORS` на измеренные значения, например:

```python
FLOORS: dict[str, float | None] = {
    "REVENUE": 1.0,
    "OTHER_OPEX": 1.0,
    "CAPEX": 0.90,
    "CONSULTING": 0.75,
    "FINANCING": 1.0,
}
```

Значения — фактическое худшее из трёх перестановок, округлённое вниз до сотых. Комментарием над словарём приписать дату замера и модель, как это сделано у `BASELINE`.

- [ ] **Step 8: Закоммитить**

```bash
uv run ruff format . && make check
git add solution/categorize_llm.py tests/test_categorize_llm.py tests/test_mutations_ledger.py
git commit -m "feat: замер восстановления по трём перестановкам, пороги по категориям"
```

---

## Итоговая проверка

- [ ] `make check` зелёный, `test_score_not_below_baseline` — 34.00 без изменений

- [ ] Греп-гейт вручную: словарь замен не протёк в `solution/`

Гейт ищет **новые** формулировки из словаря замен, а не исходные триггеры. Исходные
(`sales settlement`, `facility drawdown`, `retainer fee`) — это и есть тело правил
`solution/categorize.py`, они там законны; греп по ним даёт гарантированное ложное
срабатывание.

```bash
grep -rniE --include='*.py' "revenue recognised on customer contracts|credit line disbursement|capital acquisition of|consulting mandate for|executive consulting|upkeep and running costs|desilting" solution/ && echo "ПРОТЕЧКА" || echo "чисто"
```

Expected: `чисто` — словарь замен и мутированные описания живут только в `eval/` и `tests/`

- [ ] Публичный архив пересобирается байт в байт после правки `tools/public_archive.py` (Task 3, Step 3)
- [ ] В `docs/superpowers/specs/2026-08-06-categorization-holdout-design.md` не осталось пунктов без задачи
