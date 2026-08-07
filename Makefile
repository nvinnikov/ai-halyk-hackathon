# Все цели идут через `uv run` ради воспроизводимого окружения из uv.lock.
# `check` — локальное зеркало CI-гейта.
.PHONY: install public-archive solve score sanity grep-gate lint typecheck test check

install:
	uv sync --extra dev

# Цели зависят от install, чтобы на свежем клоне `make test`/`make lint`
# работали без отдельного шага: сам по себе `uv run` ставит только основные
# зависимости, а pytest/ruff/mypy живут в extra `dev`. Повторный sync —
# быстрый no-op, и make выполнит его один раз за вызов.

# Публичный архив в git не хранится (*.zip в .gitignore), а вход пайплайна —
# именно архив. Собирается из закоммиченного датасета тем же кодом, что зовут
# CI и tests/conftest.py. Если файл уже есть — no-op.
public-archive: install
	uv run python tools/public_archive.py

# Основной прогон через единственную точку входа: пишет out/submission.json и
# печатает скор по публичному ground_truth. Архив переопределяется:
# `make solve ARCHIVE=private.zip` — тогда public-archive не нужен.
ARCHIVE ?= 6a741640c31eb032062683.zip

solve: public-archive
	./run.sh $(ARCHIVE)

score: solve

# Первое, что запускается на новом архиве 9 августа, до пайплайна: без LLM,
# секунды. Печатает dataset_hash и диф против eval/public_baseline.json —
# каждая строка дифа читается как «что сломается».
# Снимок публичного набора перезаписывается так:
#   uv run python solution/sanity.py <архив> --write-baseline
sanity: public-archive
	uv run python solution/sanity.py $(ARCHIVE)

# Секунда на прогон: ни одного имени, порога, номера пункта или префикса
# TXN-/ACC- в solution/. Дублируется тестом test_grep_gate.py (то есть входит
# в make check), отдельная цель — чтобы гонять её одну.
grep-gate: install
	uv run python eval/grep_gate.py

lint: install
	uv run ruff format --check .
	uv run ruff check .

typecheck: install
	uv run mypy

test: install
	uv run pytest

check: lint typecheck test
