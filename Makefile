# Все цели идут через `uv run` ради воспроизводимого окружения из uv.lock.
# `check` — локальное зеркало CI-гейта. Цели ниже `check` — репетиция
# (задача 31): однословные команды на 9 августа под трёхчасовым таймером.
.PHONY: install public-archive solve score lint typecheck test check \
	run sanity eval-offline eval-live cassette-freeze determinism submit

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

lint: install
	uv run ruff format --check .
	uv run ruff check .

typecheck: install
	uv run mypy

test: install
	uv run pytest

check: lint typecheck test

# --- репетиция / боевой день (задача 31) --------------------------------------

# Однословный алиас ./run.sh: без public-archive-зависимости — 9 августа
# ARCHIVE=приватный.zip уже лежит на диске, пересборка публичного не нужна.
run: install
	./run.sh $(ARCHIVE)

sanity: install
	uv run python solution/sanity.py $(ARCHIVE)

# Без сети: инварианты (expected-режим по умолчанию) + греп-гейт + юниты.
# LLM_OFFLINE=1 — защита от случайного промаха кассеты/кэша мимо сети.
# LLM_PROVIDER=gemini — кассета заморожена под gemini-прогон (llm.py: ключ
# кэша зависит от модели, а модель — от LLM_PROVIDER); без неё все ключи
# мимо кассеты, забор мёртв. НЕ ставить на строку pytest: tests/test_faults.py
# намеренно снимает LLM_OFFLINE и мокает llm._create (anthropic-путь) для
# симуляции мёртвой сети — глобальный LLM_PROVIDER=gemini увёл бы вызов мимо
# мока в реальную сеть Gemini (проверено: живой 403 без ключа). Офлайн-гейт
# extracted-пути (tests/test_extracted_gate.py) сам ставит LLM_PROVIDER=gemini
# точечно через monkeypatch. ВАЖНО: инварианты гоняются в ОБОИХ режимах
# (ревью PR #9, 10-я волна) — extracted ПОСЛЕДНИМ, чтобы trace/ остался от
# боевого режима (режимы делят каталог трейсов, и check_fallback_rate
# обязан мерить extracted, а не expected с tier==0 по построению).
eval-offline: install
	LLM_OFFLINE=1 uv run pytest -q
	uv run python eval/grep_gate.py
	LLM_OFFLINE=1 LLM_PROVIDER=gemini uv run python eval/invariants.py $(ARCHIVE)
	LLM_OFFLINE=1 LLM_PROVIDER=gemini uv run python eval/invariants.py $(ARCHIVE) extracted

# С ключом в окружении: extraction eval, мутации (включая fx), LOBO.
eval-live: install
	uv run python eval/extraction_eval.py $(ARCHIVE)
	uv run python eval/mutations.py $(ARCHIVE) rename
	uv run python eval/mutations.py $(ARCHIVE) shift
	uv run python eval/mutations.py $(ARCHIVE) fx
	uv run python eval/lobo.py $(ARCHIVE)

# Заморозить кэш публичного extracted-прогона как кассету — регрессионный
# забор: правка промпта не сможет молча сломать извлечение (llm.py, раздел 3).
cassette-freeze:
	mkdir -p eval/cassette && cp work/llm_cache/*.json eval/cassette/

# Прогон дважды, второй целиком из кэша — байт-диф submission.json.
determinism: install
	./run.sh $(ARCHIVE) && cp out/submission.json out/.det-a.json
	./run.sh $(ARCHIVE) && diff out/.det-a.json out/submission.json

# Снапшот отправки: submission-<N>.json + cache-<N>/ + run-report-<N>.json.
submit:
	uv run python solution/submit.py
