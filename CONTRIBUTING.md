# Как участвовать

Это архив соревновательного решения (Halyk AI Challenge, август 2026), а не
развивающаяся библиотека. Баг-репорты и вопросы приветствуются; крупные
архитектурные изменения, скорее всего, не поедут в master — репозиторий ценен
тем, что числа в `README.md` и `docs/ops/` сходятся с реальным прогоном этого
кода, и правки не должны эту сходимость ломать.

## Что прислать в баг-репорте

Если нашлось воспроизводимое расхождение — с ключом публичного набора или с
прокси-ключом закрытого:

1. Заёмщик и пункт ковенанта (например, `B3` `6.1`).
2. Что ожидалось и что получилось.
3. Файл трейса из `work/<dataset_hash>/trace/<сценарий>.<пункт>.json` — в нём
   видно, каким ярусом посчитана ячейка и какие алярмы поднялись.

## Как прогнать локально

```bash
make install        # uv sync --extra dev
make check          # lint + typecheck + тесты — то же, что гоняет CI
make eval-offline   # инварианты, греп-гейт и юниты без сети
make solve          # полный прогон на открытом наборе
make private-score  # скор последнего прогона против прокси-ключа закрытого набора
```

Оба набора лежат в репозитории распакованными каталогами, архивы собираются
`make public-archive` / `make private-archive`. Ответы модели записаны в
`eval/cassette/`, поэтому весь конвейер гоняется офлайн и без ключей API:

```bash
LLM_OFFLINE=1 LLM_PROVIDER=gemini uv run pytest -m llm -q
```

## Правила правок

- Улучшили решение — поднимите порог тем же коммитом: `BASELINE` в
  `tests/test_solution.py` и `EXTRACTED_BASELINE` в
  `tests/test_extracted_gate.py`. Иначе откат назад не поймается.
- Код, идентификаторы и логи — на английском; комментарии, докстринги и
  сообщения коммитов — на русском.
- Ничего не зашивать под конкретный набор: имена заёмщиков, номера пунктов,
  пороговые числа и префиксы `TXN-`/`ACC-` в `solution/` запрещены и проверяются
  греп-гейтом (`eval/grep_gate.py`).
- Остальные ловушки — в [`CLAUDE.md`](CLAUDE.md), раздел «Конвенции и ловушки».

## Как себя вести

Конструктивно. Несогласие — нормально, недобросовестность — нет.

---

## In English

This is an archived competition solution, not an evolving library. Bug reports
and questions are welcome; large architectural changes are unlikely to be
merged — the value of this repo is that the numbers in `README.md` and
`docs/ops/` reproduce from a run of this code.

For a bug report, please include the borrower and covenant clause, expected vs
actual output, and the trace file from `work/<dataset_hash>/trace/`.

`make check` mirrors CI; `make eval-offline` runs invariants and the grep gate
with no network. Both datasets ship in the repo and the recorded cassette
(`eval/cassette/`) replays the whole pipeline offline with no API keys. If you
improve the solution, raise the score gates (`BASELINE`,
`EXTRACTED_BASELINE`) in the same commit. Code, identifiers and log messages are
in English; comments, docstrings and commit messages are in Russian. See
[`CLAUDE.md`](CLAUDE.md) for the full list of traps.

Be constructive. Disagreement is fine; bad faith is not.
