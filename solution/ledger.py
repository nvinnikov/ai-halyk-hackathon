"""Леджер-стадия: распаковка архива, устойчивый разбор CSV, категоризация.

Маршрутизация строк — по колонке account_id, не по разбору txn_id (4.1).
Грязные суммы ('n/a', пустые, мусор) не роняют прогон — уходят в dirty
и попадают в sanity-отчёт.

Артефакт леджера сырой и мультивалютный: суммы лежат в валюте платежа как
в CSV, ни одна из них не приведена к USD. Единственный легальный вход в
расчёт — solve.load_rows (в плане — scenario_inputs), который зовёт стадию
fx до любой агрегации. Складывать rows_of() напрямую нельзя: это сложение
долларов с евро.
"""

import csv
import json
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypedDict

from categorize import categorize
from categorize_llm import categorize_batch
from scindex import target_scenario_of
from stages import artifact
from util import dataset_hash, workdir


class LedgerRow(TypedDict):
    txn_id: str
    date: str
    account_id: str
    counterparty: str
    description: str
    currency: str
    amount: str
    cat: str
    cat_tier: int


# 6: правка CAT_PROMPT (передача основных средств внутри группы в CAPEX).
# Артефакт хранит категории, включая выход второго яруса, поэтому правка
# промпта обязана поднимать версию: stages.artifact инвалидирует только по
# ней, и без инкремента повторный прогон на том же архиве молча переиспользует
# старые категории — правка промпта в окне 9 августа не дала бы ничего.
LEDGER_VERSION = 6
_NA = {"n/a", "na", "none", "-", "—", "--"}


def extract_archive(archive: Path) -> tuple[str, Path]:
    ds_hash = dataset_hash(archive)
    input_dir = workdir(ds_hash) / "input"
    marker = input_dir / ".extracted"
    if not marker.exists():
        with zipfile.ZipFile(archive) as z:
            z.extractall(input_dir)
        marker.touch()
    return ds_hash, input_dir


# Колонки, по которым леджер узнаётся среди прочих CSV пакета. Названия — из
# официального формата задания, не из публичного файла: справочник курсов, лог
# выгрузки или реестр документов их не несут.
_LEDGER_COLUMNS = frozenset({"txn_id", "amount", "account_id"})


def _looks_like_ledger(path: Path) -> int:
    """Сколько обязательных колонок леджера в заголовке файла."""
    try:
        with path.open(encoding="utf-8-sig", errors="replace") as fh:
            header = fh.readline()
    except OSError:
        return 0
    names = {c.strip().strip('"').lower() for c in header.split(",")}
    return len(_LEDGER_COLUMNS & names)


def _pick_ledger(csvs: list[Path]) -> Path:
    """Леджер среди нескольких CSV: по заголовку, при равенстве — крупнейший.

    Ассерт здесь стоил бы ВСЕГО прогона (ревью перед окном): find_inputs
    зовётся раньше записи скелета, а `run.sh` идёт под `set -e`, поэтому лишний
    CSV в корне приватного пакета — справочник курсов, лог выгрузки — оставлял
    бы `out/` с файлом прошлого прогона. Брифо требует ровно один CSV, но
    полагаться на это нельзя: в публичном пакете второй CSV уже лежит, просто
    в подкаталоге.

    Выбор именно fail-open, а не «первый по алфавиту»: заголовок — свойство
    формата задания, а алфавит случаен, и `fx_reference.csv` обошёл бы
    `master_ledger_2025.csv`.
    """
    ranked = sorted(csvs, key=lambda p: (-_looks_like_ledger(p), -p.stat().st_size, p.name))
    best = ranked[0]
    if len(csvs) > 1:
        print(
            f"ALARM multiple_ledger_candidates: выбран {best.name} из "
            f"{[p.name for p in csvs]} (колонок заголовка: {_looks_like_ledger(best)})",
            flush=True,
        )
    if _looks_like_ledger(best) == 0:
        # Ни один кандидат не похож на леджер — считать будет нечего, но прогон
        # обязан дойти до записи скелета и объяснить причину в логе.
        print(f"ALARM ledger_header_unrecognised: {best.name}", flush=True)
    return best


def _pick_template(templates: list[Path]) -> Path:
    """Шаблон submission среди нескольких: по числу сценариев, затем по глубине.

    Тот же довод, что у _pick_ledger, и та же цена: ассерт здесь обнулял бы
    прогон, а лишний файл с этим именем в приватном пакете ничем не запрещён —
    заполненный пример ответа, копия в examples/, шаблон рядом с документами.

    Выбирается самый содержательный: у настоящего шаблона ключей `answers`
    больше всего, у примера-огрызка меньше. При равенстве — ближайший к корню
    архива: вложенная копия скорее вспомогательная. «Первый по алфавиту» здесь
    так же случаен, как и у леджера, и тем же способом отвергнут.
    """

    def rank(path: Path) -> tuple[int, int, str]:
        try:
            answers = json.loads(path.read_text()).get("answers", {})
            width = len(answers) if isinstance(answers, dict) else 0
        except (OSError, ValueError):
            width = 0
        return (-width, len(path.parts), path.name)

    ranked = sorted(templates, key=rank)
    best = ranked[0]
    if len(templates) > 1:
        print(
            f"ALARM multiple_templates: выбран {best.name} из {[str(p) for p in templates]}",
            flush=True,
        )
    return best


def find_inputs(input_dir: Path) -> dict:
    """Файлы датасета ищутся, а не зашиваются именами (раздел 9).

    Брифо требует ровно один CSV, но публичный набор содержит два: один в root
    (леджер), второй в documents/ (логи). Порядок поиска: CSV в root, при их
    отсутствии — rglob мимо каталогов с PDF. Неоднозначность на любом шаге
    решается _pick_ledger, а не исключением: эта функция зовётся до записи
    скелета submission, и её падение обнуляет весь прогон.
    """
    templates = sorted(input_dir.rglob("submission_template.json"))
    assert templates, "в пакете нет submission_template.json"
    template = _pick_template(templates)
    root = template.parent
    pdfs = sorted(root.rglob("*.pdf"))
    pdf_dirs = {p.parent for p in pdfs}

    csvs = sorted(root.glob("*.csv"))
    if not csvs:
        all_csvs = sorted(root.rglob("*.csv"))
        csvs = [c for c in all_csvs if c.parent not in pdf_dirs] or all_csvs
    assert csvs, "в пакете нет ни одного CSV"
    return {
        "root": root,
        "template": template,
        "ledger_csv": _pick_ledger(csvs),
        "pdfs": pdfs,
    }


def parse_amount(raw: str) -> Decimal | None:
    s = raw.strip().replace(",", "").replace(" ", "")
    if not s or s.lower() in _NA:
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if neg else d


def load_ledger(wd: Path, input_dir: Path, target_scenarios: list[str] | None = None) -> dict:
    def build() -> dict[str, list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        dirty: list[dict[str, Any]] = []
        target_set = set(target_scenarios) if target_scenarios else set()
        alarms: list[dict[str, Any]] = []

        with open(find_inputs(input_dir)["ledger_csv"], newline="") as fh:
            for r in csv.DictReader(fh):
                rec = {
                    k: (r.get(k) or "").strip()
                    for k in ("txn_id", "date", "account_id", "counterparty", "description", "currency")
                }
                # Первый ярус: категоризация по правилам
                rec["cat"] = categorize(rec["description"])
                rec["cat_tier"] = 1
                amt = parse_amount(r.get("amount") or "")
                if amt is None:
                    # Категория известна и здесь: сумму такой строке может
                    # вернуть записка казначейства (amount_override), и тогда
                    # она считается наравне с прочими.
                    dirty.append({**rec, "raw_amount": r.get("amount")})
                    continue
                rec["amount"] = str(amt)
                rows.append(rec)

        # Второй ярус: LLM для непокрытого (только для целевых заёмщиков).
        # dirty-строки участвуют наравне с rows: восстановленная через
        # amount_override строка считается в агрегатах и не должна остаться
        # OTHER только потому, что её сумма в CSV была грязной.
        if target_set:
            second_tier = rows + dirty
            descriptions = sorted(
                {
                    r["description"]
                    for r in second_tier
                    if r["cat"] == "OTHER" and target_scenario_of(r["txn_id"], target_set)
                }
            )
            if descriptions:
                llm_categories, cat_alarms = categorize_batch(descriptions)
                alarms.extend(cat_alarms)

                for r in second_tier:
                    if r["cat"] == "OTHER" and r["description"] in llm_categories:
                        if target_scenario_of(r["txn_id"], target_set):
                            r["cat"] = llm_categories[r["description"]]
                            r["cat_tier"] = 2

        rows.sort(key=lambda x: x["txn_id"])
        dirty.sort(key=lambda x: x["txn_id"])
        return {"rows": rows, "dirty": dirty, "alarms": alarms}

    # Провал LLM-категоризации (categorize_failed) не кэшируется (ревью PR #9,
    # 23-я волна): иначе расход целевого заёмщика залипал бы в OTHER навсегда,
    # а OTHER участвует в роллапах EBITDA — перезапуск после устранения причины
    # обязан перекатегоризировать.
    return artifact(
        wd / "ledger.json",
        LEDGER_VERSION,
        build,
        cache_if=lambda d: not any(a.get("kind") == "categorize_failed" for a in d["alarms"]),
    )


def rows_of(art: dict) -> list[dict]:
    return [{**r, "amt": Decimal(r["amount"])} for r in art["rows"]]


def dirty_rows_of(art: dict) -> list[dict]:
    """Строки с неразобранной суммой, amt=None.

    Считать их нельзя, но и потерять нельзя: некоторые такие строки
    восстанавливаются через amount_override из дополнительных документов. Отсев
    невосстановленных — в engine.prepare_rows, после применения фактов.
    """
    return [{**r, "amt": None} for r in art["dirty"]]
