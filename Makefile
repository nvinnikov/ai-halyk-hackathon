# Все цели идут через `uv run` ради воспроизводимого окружения из uv.lock.
# `check` — локальное зеркало CI-гейта.
.PHONY: install extract solve score lint typecheck test check

install:
	uv sync --extra dev

# Цели зависят от install, чтобы на свежем клоне `make test`/`make lint`
# работали без отдельного шага: сам по себе `uv run` ставит только основные
# зависимости, а pytest/ruff/mypy живут в extra `dev`. Повторный sync —
# быстрый no-op, и make выполнит его один раз за вызов.

# Пересобирает кэш текста PDF (solution/docs_text.json, в git не хранится).
# Нужен только для dossier.py при разборе документов; solve.py работает без него.
extract: install
	uv run python solution/extract.py

# Основной прогон: пишет solution/submission.json и печатает скор по публичному
# ground_truth.
solve: install
	uv run python solution/solve.py

score: solve

lint: install
	uv run ruff format --check .
	uv run ruff check .

typecheck: install
	uv run mypy

test: install
	uv run pytest

check: lint typecheck test
