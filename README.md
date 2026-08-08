# ai-halyk — Agentic Bank

Решение кейса: из банковского леджера за 2025 год и PDF-документов заёмщиков
считаются финансовые ковенанты (пункты 6.1 / 6.2 / 6.3) по каждому сценарию.
Результат — `out/submission.json`.

Текущий результат на публичном наборе — два числа, и путать их нельзя:

| Режим | Скор | Чем меряется |
| --- | --- | --- |
| `extracted` — **боевой**, дефолт `main()` и `run.sh` | **30.00 / 36.00 (83.3%)** | `tests/test_extracted_gate.py`, порог `EXTRACTED_BASELINE` |
| `expected` — эталонные факты и спеки из `eval/` | 34.00 / 36.00 (94.4%) | `tests/test_solution.py`, порог `BASELINE` |

9 августа поедет `extracted`: документы читает сам пайплайн, а не разметка из
`eval/expected_extraction.py`. 34.00 — это потолок расчётного ядра при идеально
извлечённых входах, то есть цена вопроса по извлечению — 4.00 балла.

## Быстрый старт

```bash
make public-archive                   # собрать публичный архив (в git не хранится)
./run.sh 6a741640c31eb032062683.zip   # единственная точка входа: архив → out/submission.json
make solve                            # то же самое через make (ARCHIVE=... переопределяет архив)
make check                            # локальный CI-гейт: lint + typecheck + tests
```

Вход пайплайна — zip-архив датасета, а `*.zip` в `.gitignore`, поэтому на свежем
клоне публичный архив надо собрать: `make public-archive` пакует
`dataset/agentic-bank-public/` в `6a741640c31eb032062683.zip`. Тем же кодом
(`tools/public_archive.py`) пользуются CI и `tests/conftest.py` — иначе байты
архива, а с ними и `dataset_hash`, разошлись бы. На боевом прогоне архив
приходит аргументом и уже существует.

Окружение поднимает [uv](https://docs.astral.sh/uv/) из `uv.lock`, Python пинится
`.python-version`. Все скрипты рассчитаны на запуск **из корня репозитория** —
пути к датасету относительные.

## Структура

| Путь | Что внутри |
| --- | --- |
| `dataset/agentic-bank-public/` | Пакет задания от организаторов: условие (`CASE.*.md`), леджер, 200 PDF (плюс CSV-лог, TXT и `Thumbs.db` в `documents/`), `ground_truth.json`, шаблон ответа. Не редактируется. |
| `solution/ledger.py`, `fx.py`, `engine.py` | Распаковка архива, устойчивый разбор и категоризация леджера (`categorize*.py`), валютная нормализация, Decimal-агрегация. |
| `solution/dsl.py`, `interp.py`, `templates.py` | Грамматика метрик, интерпретатор со знаковым вердиктом, библиотека шаблонов с сигнатурным матчем. |
| `solution/evidence.py`, `fallbacks.py` | Улика откатом документального решения; лестница фолбэков (спека → эвристика → приор). |
| `solution/pdftext.py`, `vision.py`, `route.py`, `dossier.py`, `facts_extract.py` | Документный слой: постраничный текст, vision по слепым страницам, маршрутизация, сшивка досье, факты с цитатами. |
| `solution/guard.py`, `llm.py`, `stages.py` | Защита от prompt-injection и галлюцинаций цитат; LLM-клиент с content-addressed кэшем; идемпотентные стадии. |
| `solution/solve.py` | Harness: скелет-первым `out/submission.json`, fail-open на ячейку, трейс в `work/<hash>/trace/`. |
| `eval/` | Эталоны и метрики: `expected_extraction.py` (бывшие FACTS/SPECS), приор статусов, мутации. |
| `tools/public_archive.py` | Сборка публичного архива датасета — общий код для `make public-archive`, CI и `tests/conftest.py`. |
| `docs/superpowers/specs/`, `docs/superpowers/plans/` | Дизайн-спека и план реализации пайплайна. |

## Команды

| Команда | Что делает |
| --- | --- |
| `make install` | `uv sync --extra dev` |
| `make public-archive` | Собрать `6a741640c31eb032062683.zip` из `dataset/agentic-bank-public/` |
| `make solve` | Прогнать решение (`./run.sh`), записать `out/submission.json`, напечатать скор |
| `make lint` | `ruff format --check` + `ruff check` |
| `make typecheck` | `mypy` |
| `make test` | `pytest` |
| `make check` | Всё вышеперечисленное — зеркало CI |

## CI

- **`.github/workflows/ci.yml`** — на push и PR в `master`: lint, typecheck,
  тесты и end-to-end прогон `./run.sh`, плюс сканирование секретов
  (gitleaks, конфиг в `.gitleaks.toml`).
- **`.github/workflows/claude-review.yml`** — автоматический review PR. Требует
  секрет репозитория `CLAUDE_CODE_OAUTH_TOKEN`; без него джоба скипается.
  **Правила ревью — в `.github/REVIEW.md`**, тюнить надо там: правка самого
  workflow отключает ревью на том же PR (защита `claude-code-action`).
- **`.github/workflows/claude.yml`** — ассистент по упоминанию `@claude`
  в комментариях к PR и issue.

Регрессионных порогов скора два, и оба входят в `make check`:
`BASELINE` в `tests/test_solution.py` стережёт expected-режим (расчётное ядро),
`EXTRACTED_BASELINE` в `tests/test_extracted_gate.py` — боевой extracted-путь
(офлайн, из замороженной кассеты `eval/cassette/`). Улучшили решение — поднимите
соответствующий порог тем же коммитом, тогда откат назад поймается сразу.
