# ai-halyk — Agentic Bank

Решение кейса: из банковского леджера за 2025 год и PDF-документов заёмщиков
считаются финансовые ковенанты (пункты 6.1 / 6.2 / 6.3) по каждому сценарию.
Результат — `solution/submission.json`.

Текущий результат на публичном наборе: **34.00 / 36.00 (94.4%)**.

## Быстрый старт

```bash
make solve     # посчитать ответ и вывести скор против ground_truth
make check     # локальный CI-гейт: lint + typecheck + tests
```

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
| `solution/solve.py` | Сборка `submission.json` + скоринг против `ground_truth.json`. |
| `docs/superpowers/specs/` | Проектная спека пайплайна. |

## Команды

| Команда | Что делает |
| --- | --- |
| `make install` | `uv sync --extra dev` |
| `make extract` | Пересобрать кэш текста PDF |
| `make solve` | Прогнать решение, записать `submission.json`, напечатать скор |
| `make lint` | `ruff format --check` + `ruff check` |
| `make typecheck` | `mypy` |
| `make test` | `pytest` |
| `make check` | Всё вышеперечисленное — зеркало CI |

## CI

- **`.github/workflows/ci.yml`** — на push и PR в `master`: lint, typecheck,
  тесты и end-to-end прогон `solution/solve.py`, плюс сканирование секретов
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
