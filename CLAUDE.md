# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## О репозитории

Решение кейса Agentic Bank (Halyk AI Challenge). Из леджера за 2025 год
(`master_ledger_2025.csv`) и PDF-документов заёмщиков считаются финансовые
ковенанты (пункты 6.1 / 6.2 / 6.3 по каждому сценарию), результат — `out/submission.json`.

Приватный датасет открывается 9 августа 2026 в 11:00 (Астана), дедлайн 14:00 —
окно 3 часа. Отсюда все решения в коде: воспроизводимость, fail-open, отсутствие
хардкода под публичный набор.

Скоринг ячейки (`CASE.ru.md`, раздел 4): 0.50 за `status`, 0.30 за `actual`
(`0.30 × max(0, 1 − e/0.05)`, `e` — относительная погрешность), 0.20 за
`evidence_txn_id` (при `null` в ключе баллы убывают вместе с `actual`). Неверный
`status` обнуляет ячейку целиком. Максимум на публичном наборе — 36.00.

## Команды

```bash
make public-archive     # собрать публичный архив из dataset/ (в git не хранится)
./run.sh <архив.zip>    # единственная точка входа: архив → out/submission.json
make solve              # то же через make (ARCHIVE=... переопределяет архив)
make check              # локальное зеркало CI: lint + typecheck + test
make install            # uv sync --extra dev
```

Репетиция и боевой день (задача 31 — однословные команды под таймером):

| Команда | Что делает |
| --- | --- |
| `make run` | `./run.sh $(ARCHIVE)` без пересборки публичного архива |
| `make sanity` | `solution/sanity.py` — без LLM, секунды: диф против `eval/public_baseline.json` |
| `make eval-offline` | `LLM_OFFLINE=1 pytest` + греп-гейт + инварианты в **обоих** режимах |
| `make eval-live` | с ключом: extraction eval, мутации (`rename`/`shift`/`fx`), LOBO |
| `make determinism` | два прогона, второй из кэша — байт-диф `submission.json` |
| `make cassette-freeze` | `work/llm_cache/*.json` → `eval/cassette/` как регрессионный забор |
| `make submit` | снапшот отправки: `submission-<N>.json` + `cache-<N>/` + `run-report-<N>.json` |

Один тест: `uv run pytest tests/test_solution.py::test_deterministic`.

Вход пайплайна — zip-архив, а `*.zip` в `.gitignore`, поэтому на свежем клоне
архив надо собрать: `make public-archive` пакует `dataset/agentic-bank-public/`
в `6a741640c31eb032062683.zip`. Тем же кодом (`tools/public_archive.py`)
пользуются CI и `tests/conftest.py` — иначе байты архива, а с ними и
`dataset_hash`, разошлись бы.

**Всё запускается из корня репозитория** — пути к датасету относительные
(`dataset/agentic-bank-public/...`). Окружение — `uv` из `uv.lock`, Python пинится
`.python-version` (3.12).

Тесты с маркером `llm` ходят в API и в `make check` не входят (`addopts = -m 'not llm'`).

### Переменные окружения

`run.sh` сорсит `.env`, если он есть (сам код `.env` не читает). Читаются:
`ANTHROPIC_API_KEY` / `GEMINI_API_KEY`, `LLM_PROVIDER` (`anthropic` | `gemini`),
`LLM_OFFLINE=1` (промах кэша и кассеты = ошибка, а не сетевой вызов),
`LLM_BUDGET_USD` (дефолт 50), `GEMINI_MIN_INTERVAL_MS`, `SOLVE_WORKERS`
(дефолт 4), `TEAM_NAME` / `CONTACT_EMAIL` / `MODEL_NAME` (реквизиты
submission).

## Архитектура

Расчётный поток: архив → леджер (`ledger`/`categorize*`) → индекс (`scindex`) →
валютная нормализация (`fx`) → Decimal-агрегация (`engine`) → метрика в DSL
(`dsl`/`interp`/`templates`) → вердикт → улика (`evidence`) → лестница фолбэков
(`fallbacks`).

Документный поток (**боевой путь, дефолт с задачи 24**): PDF →
`pdftext`/`vision` → `route` → `dossier` → `facts_extract` / `specs_extract` →
те же спеки и факты, что раньше приходили из эталона.

- **`solve.py`** — harness и единственная точка входа. `main(archive,
  facts_source="extracted", hide_templates=frozenset())` печатает `dataset_hash`
  первой строкой, кладёт скелет submission сразу после чтения шаблона и
  перезаписывает по одной ячейке — на любой секунде прогона
  `out/submission.json` валиден. Любой сбой fail-open: `ALARM` в лог, ячейка
  приходит лестницей, прогон продолжается; диагностика (borrower-трейс,
  `sign_divergence`, `cell_other_alarm`, run-report) не может стоить ячейки.
  Ядро — `run_cell`. `facts_source="expected"` — эталон через мост
  `legacy_spec_to_cellspec` + `eval/expected_extraction.py` (регрессия и eval);
  `"extracted"` — документный конвейер. `hide_templates` — для LOBO.
- **`ledger.py`** — распаковка архива, устойчивый разбор сумм (грязные суммы
  уходят в `dirty`, не роняя прогон), категоризация: `categorize.py` (регулярки,
  ярус 1) + `categorize_llm.py` (LLM по непокрытому, ярус 2). Артефакт
  **сырой и мультивалютный** — легальный вход в расчёт только через
  `solve.load_rows`, который зовёт `fx` до любой агрегации.
- **`scindex.py`** — единственное место разбора `txn_id`: индекс
  txn_id → scenario_id → account_id. Ровно одно попадание целевого id →
  сценарий, иначе алярм. Фоновые счета считаются, но ошибкой не являются.
- **`fx.py`** — нормализация в USD до агрегации; лестница: свой курс → курс
  другого целевого заёмщика → строка исключается с алярмом. Подстановка `1.0`
  ступенью лестницы не является никогда.
- **`taxonomy.py`** — двухуровневая таксономия (листья + роллапы). `OTHER` —
  корзина неразнесённого, в `OPEX_TOTAL` не входит (иначе тихо завышает
  EBITDA), в `ALL` входит. `cell_other_alarm` — поячеечный алярм потери строки.
- **`engine.py`** — Decimal-агрегация `agg(rows, category, sign, pred)`,
  `prepare_rows` (факты досье + откаты для контрфактуалов), related-матч по
  токенам `is_related`.
- **`dsl.py` / `interp.py` / `templates.py`** — грамматика метрик, интерпретатор
  со знаковым вердиктом и триггером, библиотека 19 шаблонов. Основной путь
  матча — `match_heading` (заголовок пункта однозначен), резерв —
  сигнатурный матч (знак в сигнатуре затёрт).
- **`evidence.py`** — улика откатом документального решения по его типу
  (reclass / inclusion / exclusion / amount_fix); ровно один переворачивающий
  кандидат → улика, иначе `null` — и это правильный ответ, а не пробел.
- **`fallbacks.py`** — лестница: спека → эвристика по цитате → приор
  ((направление, семья) → номер пункта → глобальный); `actual` = порог или
  медиана посчитанных. `null` в `actual` не существует как состояние.
- **Документный слой:** `pdftext.py` (постраничный текст, слепота = <200
  символов И <3 чисел), `vision.py` (слепые страницы одностраничными PDF),
  `route.py` (строгая привязка к счёту; фоновый документ — карантин без алярма
  и без LLM), `dossier.py` (действующая редакция: маркер перебивает дату; пул
  потоков `SOLVE_WORKERS`, fail-open на документ и на заёмщика),
  `facts_extract.py` (факты с цитатами), `specs_extract.py` (пункт → спека;
  грамматика и guard прогоняются заново при каждом `extract_specs`, а не
  однократно при извлечении).
- **`guard.py`** — контракт для всех LLM-потребителей: документ в промпт только
  через `sanitize_document`, `DATA_NOT_COMMANDS` в промпте, каждая цитата —
  через `verify_quote` (провал → алярм `quote_unverified`, факт отбрасывается).
- **`llm.py`** — content-addressed кэш, ретраи, бюджет. Порядок чтения:
  `eval/cassette/<key>.json` → `work/llm_cache/<key>.json` → сеть. Провайдер
  через `LLM_PROVIDER`; модель входит в ключ кэша, поэтому кэши anthropic и
  gemini не пересекаются. Джиттер backoff — из ключа, не из `random()`.
  Поле `thinking` не передаётся. **Ручная правка содержимого кэша запрещена.**
- **`stages.py`** — идемпотентность стадий: артефакт переиспользуется, если
  совпала версия стадии. `cache_if` не даёт запечь деградированный fail-open
  результат на диск.
- **`util.py`** — `dataset_hash`, `workdir`, `stable_json`, `q2` (ROUND_HALF_UP).
- **`score.py`** — скорер по официальной формуле, без вариантов.
- **`sanity.py`** — без LLM, секунды: диф текущего архива против
  `eval/public_baseline.json` — готовый список того, что сломается на новом
  наборе.
- **`submit.py`** — снапшот отправки: `submission.json` + ровно тот кэш, что в
  него лёг, + run-report под одним номером `N`.

### Артефакты на диске

| Путь | Что |
| --- | --- |
| `work/<dataset_hash>/{text,vision,route,dossier,facts,specs,sanity}/` | Артефакты стадий, версионированные `stages.artifact` |
| `work/<dataset_hash>/index.json` | Индекс сценариев и счетов |
| `work/<dataset_hash>/trace/<сценарий>.<пункт>.json` | Трейс ячейки; имя разбирать как `stem.split(".", 1)`, не `rsplit` |
| `work/<dataset_hash>/trace/<сценарий>.borrower.json` | Borrower-трейс (покрытие категорий) |
| `work/llm_cache/*.json` | Content-addressed кэш LLM (общий между наборами) |
| `out/submission.json` | Ответ; `out/run-report.json` — версии стадий, бюджет, `tier_breakdown`, `alarm_counts`, `git_sha` |

## Eval-слой (`eval/`)

- **`expected_extraction.py`** — размеченный вручную эталон (`FACTS`, `SPECS`):
  12 заёмщиков + 36 пар (метрика, порог). Вопрос к LLM-слою измерим:
  восстанавливает ли он этот файл из PDF.
- **`extraction_eval.py`** — сравнение извлечённого с эталоном (имена —
  токенами, числа — Decimal).
- **`grep_gate.py`** — запрещённые литералы в `solution/` и `run.sh`; список
  строится из eval-данных и шаблона, не хардкодится.
- **`invariants.py`** — дешёвые детерминированные проверки поверх уже
  посчитанных артефактов (`check_sum_conservation`, `check_evidence_provenance`,
  `check_breach_evidence`, `check_fallback_rate`, `check_dossier_binding`, …).
  Вход пересобирается заново через `solve.scenario_inputs`, поэтому сравнение с
  записанным submission не тавтологично.
- **`mutations.py`** — мутация датасета целиком + сквозной extracted-прогон:
  `rename` (ответы обязаны совпасть байт в байт), `shift` (новый статус выводится
  без нашего DSL), `fx` (пайплайн обязан восстановить исходные USD).
  Guard от холостой мутации: замена не встретилась → `RuntimeError`, не зелёный тест.
- **`mutations_text.py`** — мутация только текста договора (один слой, спеки).
- **`mutations_ledger.py`** — holdout описаний для пяти категорий, у которых его
  нет в данных; замены обязаны быть нейтральными (не содержать имени категории).
- **`lobo.py`** — 12 прогонов, каждый раз один заёмщик без шаблонов. Дельта
  около нуля — **хороший** результат: библиотека не подменяет извлечённое.
- **`prior.py` / `prior.json`** — эмпирический приор статусов из публичного ключа.
- **`public_baseline.json`** — снимок публичного набора для `sanity`.

## Конвенции и ловушки

- Модули `solution/*` делают `sys.path.insert(0, "solution")` и импортируют друг
  друга плоско — репозиторий не пакет (`[tool.uv] package = false`). Ruff-правило
  `E402` отключено именно поэтому. `tests/conftest.py` фиксирует `cwd` и `sys.path`
  и autouse-фикстурой изолирует `solve.OUT` от боевого `out/`.
- `dataset/agentic-bank-public/` — пакет от организаторов, **не редактируется**.
- **Два регрессионных порога, оба поднимать тем же коммитом:**
  `BASELINE = 34.00` в `tests/test_solution.py` (expected-режим) и
  `EXTRACTED_BASELINE = 29.5` в `tests/test_extracted_gate.py` (боевой
  extracted-режим на прогретом чекауте, `LLM_OFFLINE=1`; на холодном CI —
  честный skip). Живой floor extracted-прогона (≥30.00 и ≥30 ячеек ярусом dsl) —
  в `tests/test_extracted_run.py` под маркером `llm`.
- **Правка build-логики стадии обязана поднять её `*_VERSION`.** Иначе старый
  артефакт молча переиспользуется. Обратная сторона: подъём версии форсирует
  повторное извлечение и жжёт бюджет — см. `docs/ops/activation-step.md` и
  `docs/ops/recovery-playbook.md` (деградировавший артефакт залипает на диске
  после сбоя LLM).
- Форма ответа сверяется с `submission_template.json` (инвариант проверяется в
  `dump_submission` перед каждой записью); `main()` должен быть детерминирован
  (`test_deterministic`).
- Известные грабли: `round()` — банковское округление (для `actual` нужен
  `util.q2` / `Decimal.quantize(ROUND_HALF_UP)`); итерация по `set` и
  суммирование `float` в порядке словаря ломают воспроизводимость — сортировать
  перед использованием; деньги — только `Decimal`; `random()` и `time.time()`
  в логике запрещены.
- Смена `LLM_PROVIDER` не инвалидирует артефакты стадий (версия стадии не знает
  модель) — смешанный по провайдерам workdir даёт содержательную, не случайную
  разницу; диагноз разобран в `docs/ops/fresh-workdir-noise-diagnosis.md`.
- Код, идентификаторы и логи — на английском; комментарии, докстринги и
  сообщения коммитов — на русском.

## Инварианты дизайна

Спека: `docs/superpowers/specs/2026-08-06-halyk-pipeline-design.md`,
план (31 задача): `docs/superpowers/plans/2026-08-06-halyk-pipeline.md`.
Вторая волна: `docs/superpowers/specs/2026-08-06-categorization-holdout-design.md`
+ `docs/superpowers/plans/2026-08-07-categorization-holdout.md`.
Отчёты и плейбуки прогонов — `docs/ops/`.

- **«LLM читает, код считает».** Модель трогает только то, что требует понимания
  текста; её выход всегда проходит JSON-схему и грамматику до попадания в расчёт.
  Арифметику (EBITDA, проценты, доли) модель не делает никогда.
- **Детерминизм через content-addressed кэш**, а не через `temperature=0`. Ключ =
  `sha256(model_id + prompt + json_schema + schema_version)`. Кэш не
  инвалидируется по времени, только по содержимому; «заморозка» — это снимок
  каталога кэша рядом с отправляемым JSON (`make cassette-freeze`, `make submit`),
  а не запрет на правки.
- **Греп-гейт:** ни одного имени заёмщика, номера пункта (`6.1`), порогового
  числа, префикса `TXN-`/`ACC-` в `solution/` — только в `tests/` и `eval/`.
  Ничего не зашивать под 3 пункта на заёмщика, 12 сценариев, `USD`/`EUR`,
  русский язык, `ACC-\d{4}`.
- **Скелет-первым и fail-open на ячейку.** Пустая и неверная ячейка стоят
  одинаково — ноль, поэтому нерасчитанной ячейки не бывает: её закрывает
  лестница. Диагностика оборачивается `try/except` наравне с соседями.

## CI и review

- `.github/workflows/ci.yml` — lint, typecheck, pytest, end-to-end `./run.sh`
  (под `LLM_OFFLINE=1`), gitleaks (конфиг `.gitleaks.toml`).
- `.github/workflows/claude-review.yml` — автоматический review PR. **Правила
  ревью — в `.github/REVIEW.md`**, тюнить надо там: правка самого workflow
  отключает ревью на том же PR (защита `claude-code-action`).
- `.github/workflows/claude.yml` — ассистент по упоминанию `@claude`.
