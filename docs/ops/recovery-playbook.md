# Recovery playbook: отравленные артефакты после сбоя LLM

Главный урок двух живых инцидентов на этом воркtree (429/исчерпанный баланс
во время боевого extracted-прогона и во время неудавшейся попытки активации
`FACTS_VERSION`, см. `debug-extracted-report.md`, разделы про корень 5 и
"Что именно активирует подъём FACTS_VERSION"): **повторный прогон после сбоя
не гарантирует починку** — деградировавший результат может залипнуть на
диске и молча вернуться на любом следующем запуске, даже когда причина сбоя
(баланс, сеть, rate limit) уже устранена.

## Механизм

`stages.artifact(path, version, build)` (`solution/stages.py`) переиспользует
файл на диске, если у него совпала версия стадии — и ТОЛЬКО версия:

```python
if path.exists():
    data = json.loads(path.read_text())
    if data.get("_meta", {}).get("stage_version") == version:
        return data          # неважно, успешный это результат или фолбэк
```

Если `build()` внутри поймал ошибку модели и вернул **деградировавший, но
валидный по форме** dict (пустые факты, `covenants: []`, пустое досье) —
этот dict проходит `artifact()` как обычный успех, ложится на диск под
текущей версией стадии и будет отдаваться повторно на каждом следующем
прогоне того же `work/<hash>`, пока версия стадии не изменится или файл не
будет удалён руками.

### Кто ловит ошибку и потому подвержен, кто — нет

> **АКТУАЛИЗАЦИЯ (волны 18–28 ревью PR #9).** Описанный ниже механизм
> залипания ЗАКРЫТ: (а) `anthropic.BadRequestError` (400, включая
> "insufficient credit balance") больше НЕ заворачивается в `SchemaRejected`
> — пробрасывается наружу и артефакт не пишется; (б) все пять стадий
> (`ledger`, `route`, `dossier`, `facts`, `specs`) передают в
> `stages.artifact` предикат `cache_if` — результат с алярмом деградации
> (`categorize_failed`, `meta_extraction_failed`, `routing_failed`,
> `facts_extraction_failed`, `no_documents`, `specs_extraction_failed`,
> `no_agreement`, `doc_fact_resolve_failed`) возвращается вызывающему, но на
> диск не ложится — перезапуск честно перепытается. Раздел сохранён как
> описание класса проблемы для диагностики незнакомых артефактов.

Исторически: три стадии (`route`, `facts_extract`, `specs_extract`) ловили
**только `llm.SchemaRejected`** внутри своего `build()`; ловится →
деградировавший результат ВОЗВРАЩАЛСЯ из `build()` → кэшировался.
`dossier.py` ловил `except Exception` целиком — тоже кэшировался.

| Стадия | Артефакт | Версия | Что ловится и превращается в кэш | Alarm kind внутри артефакта |
|---|---|---|---|---|
| `route.route_doc` | `work/<hash>/route/<doc_hash>.json` | `ROUTE_VERSION` | `llm.SchemaRejected` (META/WHOSE) | `meta_extraction_failed`, `quote_unverified` |
| `dossier.build_dossiers` | `work/<hash>/dossier/<account>.json` | `DOSSIER_VERSION` | `except Exception` целиком (шире прочих) | `dossier_build_failed` |
| `facts_extract.extract_facts` | `work/<hash>/facts/<account>.json` | `FACTS_VERSION` | `llm.SchemaRejected` (по документу, цикл продолжается) | `facts_extraction_failed` |
| `specs_extract.extract_specs` | `work/<hash>/specs/<account>.json` | `SPECS_STAGE_VERSION` | `llm.SchemaRejected` | `specs_extraction_failed`, `no_agreement` (последнее — не LLM-сбой, а маршрутизация: агримента не нашлось) |

**Что НЕ поймано и потому НЕ отравляет кэш**: `llm.BudgetExhausted` и
исчерпание ретраев на транзиентных `anthropic.RateLimitError` /
`APIConnectionError` / `InternalServerError` / `APITimeoutError` — эти классы
не входят в перечисленные `except`, вылетают из `build()` наружу, `artifact()`
ничего не пишет на диск (нет try/except вокруг `build()` в `stages.py`), и
`solve._extracted_inputs`/её собственный per-scenario try/except ловит уже
здесь, на уровне `solve.py`, отдавая пустые факты БЕЗ записи файла — этот
случай самовосстанавливается: следующий прогон честно попробует заново,
файла-заглушки нет. Поэтому "оборвана сеть посреди прогона" (`llm._create`
роняет `APIConnectionError`) НЕ создаёт отравленных артефактов — только
`llm.SchemaRejected` (в первую очередь 400 от биллинга/схемы) создаёт.

## Обнаружение: что искать (офлайн, без LLM)

Все четыре вида алярмов из таблицы выше видны через:

```bash
make sanity ARCHIVE=<архив>      # печатает stage_alarms — сводку по видам
```

или руками, по конкретному `work/<hash>`:

```bash
grep -rl '"kind": "facts_extraction_failed"' work/<hash>/facts/
grep -rl '"kind": "specs_extraction_failed"' work/<hash>/specs/
grep -rl '"kind": "no_agreement"'            work/<hash>/specs/
grep -rl '"kind": "meta_extraction_failed"'  work/<hash>/route/
grep -rl '"kind": "dossier_build_failed"'    work/<hash>/dossier/
```

После полного прогона `solve.main()` то же самое доступно программно в
`out/run-report.json["alarm_counts"]` (задача 31 — `solve._alarm_counts`,
и её сестра `eval.invariants._collect_report_alarms` для `eval/invariants.py`):
обе функции обходят `route/`, `dossier/`, `facts/`, `specs/` и index/trace,
считая `kind` по всем найденным алярмам. Ненулевые значения любого из пяти
kind выше на приватном прогоне — сигнал остановиться и разобрать конкретный
файл/заёмщика, а не «списать на шум».

## Исправление: точечный пересчёт vs версия vs нюк

Порядок предпочтений — от самого дешёвого к самому дорогому:

1. **Точечно** (рекомендуется в 99% случаев под таймером): удалить ровно
   отравленный файл — `rm work/<hash>/facts/<account>.json` (или
   `specs/<account>.json`, `route/<doc_hash>.json`, `dossier/<account>.json`)
   — и перезапустить `./run.sh <архив>`. `stages.artifact` увидит отсутствие
   файла и вызовет `build()` заново ТОЛЬКО для этого заёмщика/документа; всё
   остальное read из кэша как было. Дешевле всего по деньгам и по риску
   недетерминизма (см. ниже).
2. **Версия стадии** (`ROUTE_VERSION`/`DOSSIER_VERSION`/`FACTS_VERSION`/
   `SPECS_STAGE_VERSION` в соответствующем модуле) — форсирует пересборку
   ВСЕХ артефактов стадии на этом `work/<hash>`, не только отравленных.
   Осмысленно, когда правка кода стадии затронула все документы одинаково
   (пример — `activation-step.md`: смена `TEXT_VERSION` меняет вход
   `facts_extract`/`route` для всех документов сразу). НЕ используется для
   точечной починки одного заёмщика — это дороже и без пользы пересчитывает
   и то, что уже было верным.
3. **Полный нюк** (`rm -rf work/<hash>`) — последняя инстанция, когда
   непонятно, что именно отравлено, или воркdir подозревается в более
   глубокой порче (например, смешение `facts_source` на одном архиве — см.
   "Побочная находка" в `debug-extracted-report.md`). Пересчитывает всё с
   нуля, самый дорогой вариант по деньгам и времени.

### Риск, отдельный от денег: недетерминизм повторного вызова

Кэш `llm.call` — контентно-адресный (`sha256(model + prompt + schema +
schema_version)`), НЕ `temperature=0`. Живой повторный вызов на ТОТ ЖЕ
промпт не гарантирует тот же ответ модели (живое наблюдение — см.
`activation-step.md`, раздел "Риск НЕ бюджетный, а качества": факт `fx_rate`
исчез при повторном вызове того же документа). Значит точечный пересчёт —
это не только трата бюджета, но и лотерея: часть уже рабочих фактов может
пропасть, часть отравленных — починиться, в непредсказуемой пропорции. После
любого пересчёта — свежий скор, а не молчаливое доверие к тому, что стало
лучше.

## Проверка чистоты после починки

```bash
make sanity ARCHIVE=<архив>                 # stage_alarms по всем видам == {}
LLM_OFFLINE=1 uv run python eval/invariants.py <архив> extracted
```

`invariants.py` печатает `ALARM ...` по каждому найденному алярму (включая
facts/specs после этой правки) и завершается ненулевым кодом, если
`check_fallback_rate`/прочие инварианты не проходят. Пустой список алярмов
целевых kind — единственный надёжный сигнал «отравы больше нет»; «прогон
не упал» самим по себе НЕ доказательство (fail-open по конструкции не роняет
прогон на отравленном артефакте).

## Как это связано с activation-step.md

Активационный шаг (подъём `ROUTE_VERSION`/`FACTS_VERSION`) — частный, заранее
спланированный случай пункта 2 выше: сознательная пересборка ВСЕХ артефактов
стадии, а не починка отравы. Тот же риск недетерминизма из этого документа
там уже учтён отдельным разделом — сверяться с ним перед активацией.
