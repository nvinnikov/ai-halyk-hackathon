# Все цели идут через `uv run` ради воспроизводимого окружения из uv.lock.
# `check` — локальное зеркало CI-гейта. Цели ниже `check` — репетиция
# (задача 31): однословные команды на 9 августа под трёхчасовым таймером.
.PHONY: install public-archive solve score lint typecheck test check \
	run sanity eval-offline eval-live cassette-freeze determinism submit \
	require-archive require-private-archive

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
DEFAULT_ARCHIVE := 6a741640c31eb032062683.zip
ARCHIVE ?= $(DEFAULT_ARCHIVE)

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
#
# ARCHIVE здесь ОБЯЗАТЕЛЕН, в отличие от solve: у переменной есть дефолт на
# публичный зип, и забытое `ARCHIVE=...` в окне молча посчитало бы публичный
# набор и перезаписало out/submission.json — ни одна проверка об этом не
# скажет, потому что прогон формально успешен. Целям репетиции дефолт не нужен:
# архив 9 августа называют явно.
#
# Гейта два, потому что у целей разная цена ошибки. Sanity только печатает и
# ничего не пишет; run перезаписывает отправляемый out/submission.json.
#
# require-archive (sanity) — критерий ПРОИСХОЖДЕНИЕ переменной, а не значение.
# Сравнение значения с дефолтом не отличало «забыли ARCHIVE» от «назвали
# публичный архив намеренно», и предполётная проверка стоп-строки из ранбука
# (`make sanity ARCHIVE=<публичный>`) падала вместо того, чтобы отработать —
# единственная строка, проверяющая живость стоп-проверки, не исполнялась.
# `origin == file` означает ровно одно: значение пришло из строки `ARCHIVE ?=`
# выше, то есть переменную не задали. Аргумент (`command line`) и окружение
# (`environment`, форма `export ARCHIVE=...` из ранбука — `?=` его не
# перебивает) проходят оба.
require-archive:
	@test "$(origin ARCHIVE)" != "file" || { \
	  echo "ARCHIVE не задан: целям run/sanity/determinism нужен явный архив."; \
	  echo "  make <цель> ARCHIVE=/путь/к/архиву.zip"; \
	  echo "  публичный набор гоняется через 'make solve'"; \
	  exit 1; }

# require-private-archive (run) — происхождение И значение: у run публичный
# архив не бывает верным ни при каком раскладе, публичный набор гоняется через
# `make solve`. Одного происхождения мало (ревью PR #18): `export
# ARCHIVE=<публичный>`, оставшийся в оболочке с репетиции, прошёл бы гейт, и
# out/submission.json оказался бы перезаписан результатом по публичному набору
# — ровно тот молчаливый сбой, ради которого гейт и ставился. Стоп-проверка
# sanity.py по dataset_hash сработала бы, но уже после перезаписи.
#
# Сравнение по СОДЕРЖИМОМУ, а не по имени файла (ревью PR #18, круг 6).
# `6a741640c31eb032062683.zip` — имя, которым организаторы раздали публичный
# набор (tools/public_archive.py), и ничто не обещает, что приватный приедет
# под другим. Гейт по имени отказал бы 9 августа на НАСТОЯЩЕМ архиве, да ещё
# и посоветовал бы считать публичный набор, — а ложный красный в окне дороже
# пропуска, содержательные проверки всё равно стоят дальше по пути
# (sanity.py по dataset_hash, solve._is_public_dataset внутри прогона, отказ
# submit на выходе). cmp попутно ловит копию публичного архива под любым
# именем и в любом каталоге — а это и есть публичный набор.
#
# Отсутствие любого из файлов cmp считает расхождением (exit 2), то есть гейт
# пропускает: свежий клон без собранного публичного архива не должен мешать
# боевому прогону, а несуществующий ARCHIVE поймает run.sh своим сообщением.
require-private-archive: require-archive
	@cmp -s "$(ARCHIVE)" "$(DEFAULT_ARCHIVE)" && { \
	  echo "ARCHIVE побайтово совпал с публичным архивом: run перезапишет out/submission.json."; \
	  echo "  публичный набор гоняется через 'make solve'"; \
	  echo "  если это и есть боевой архив — зовите ./run.sh <архив> напрямую"; \
	  exit 1; } || true

run: install require-private-archive
	./run.sh $(ARCHIVE)

sanity: install require-archive
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
# Гейт здесь require-archive, а не require-private-archive (ревью PR #18,
# круг 2): цель зовёт ./run.sh и точно так же перезаписывает отправляемый
# out/submission.json, поэтому забытое ARCHIVE= ей запрещено — но публичный
# архив ей как раз разрешён, это репетиционная цель, и мерить детерминизм
# больше не на чем. Опасен здесь молчаливый дефолт, а не публичный набор.
determinism: install require-archive
	./run.sh $(ARCHIVE) && cp out/submission.json out/.det-a.json
	./run.sh $(ARCHIVE) && diff out/.det-a.json out/submission.json

# Снапшот отправки: submission-<N>.json + cache-<N>/ + run-report-<N>.json.
submit:
	uv run python solution/submit.py
