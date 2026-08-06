# ai-halyk — Agentic Bank

Решение кейса: из банковского леджера за 2025 год и PDF-документов заёмщиков
считаются финансовые ковенанты (пункты 6.1 / 6.2 / 6.3) по каждому сценарию.
Результат — `out/submission.json`.

Текущий результат на публичном наборе: **34.00 / 36.00 (94.4%)**.

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
| `dataset/agentic-bank-public/` | Пакет задания от организаторов: условие (`CASE.*.md`), леджер, 203 PDF, `ground_truth.json`, шаблон ответа. Не редактируется. |
| `solution/extract.py` | Вытаскивает текст из всех PDF в кэш `solution/docs_text.json` (в git не хранится, пересобирается `make extract`). |
| `solution/dossier.py` | Маршрутизация документов по заёмщикам, поиск значимых разделов, отсев устаревших версий. |
| `solution/facts.py` | Факты, извлечённые из досье: реклассификации, связанные стороны, FX, отсечения периода. Вход для расчёта. |
| `solution/categorize.py` | Категоризация транзакций по назначению платежа. |
| `solution/engine.py` | Загрузка и нормализация леджера, агрегаты (выручка, расходы по категориям, платежи связанным сторонам). |
| `solution/covenants.py` | Формулы ковенантов и их пороги по заёмщикам. |
| `solution/solve.py` | Harness: скелет-первым `out/submission.json`, fail-open на ячейку, трейс в `work/<hash>/trace/`. |
| `tools/public_archive.py` | Сборка публичного архива датасета — общий код для `make public-archive`, CI и `tests/conftest.py`. |
| `docs/superpowers/specs/` | Проектная спека пайплайна. |

## Команды

| Команда | Что делает |
| --- | --- |
| `make install` | `uv sync --extra dev` |
| `make public-archive` | Собрать `6a741640c31eb032062683.zip` из `dataset/agentic-bank-public/` |
| `make extract` | Пересобрать кэш текста PDF |
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

Регрессионный порог скора зафиксирован в `tests/test_solution.py` (`BASELINE`).
Улучшили решение — поднимите порог тем же коммитом, тогда откат назад поймается
сразу.
