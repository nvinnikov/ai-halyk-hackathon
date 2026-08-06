"""Леджер-стадия: распаковка архива, устойчивый разбор CSV, категоризация.

Маршрутизация строк — по колонке account_id, не по разбору txn_id (4.1).
Грязные суммы ('n/a', пустые, мусор) не роняют прогон — уходят в dirty
и попадают в sanity-отчёт.
"""

import csv
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypedDict

from categorize import categorize
from categorize_llm import categorize_batch
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


LEDGER_VERSION = 3
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


def find_inputs(input_dir: Path) -> dict:
    """Файлы датасета ищутся, а не зашиваются именами (раздел 9).

    Брифо требует ровно один CSV, но публичный набор содержит два CSV:
    один в root (ledger), второй в documents/ (логи). Логика выбора:
    (1) если ровно один CSV в root — берём его;
    (2) если нет — фолбэк на rglob, исключив файлы в каталогах с PDF;
    (3) иначе (несколько CSV любом уровне) — AssertionError.
    """
    templates = sorted(input_dir.rglob("submission_template.json"))
    assert len(templates) == 1, f"шаблонов найдено {len(templates)}"
    root = templates[0].parent
    pdfs = sorted(root.rglob("*.pdf"))
    pdf_dirs = {p.parent for p in pdfs}

    # Попытка 1: CSV только в root
    csvs = sorted(root.glob("*.csv"))
    if len(csvs) == 1:
        return {
            "root": root,
            "template": templates[0],
            "ledger_csv": csvs[0],
            "pdfs": pdfs,
        }
    if len(csvs) > 1:
        raise AssertionError(f"в root найдено {len(csvs)} CSV: {csvs}")

    # Попытка 2: фолбэк — rglob исключив каталоги с PDF
    all_csvs = sorted(root.rglob("*.csv"))
    csvs = [c for c in all_csvs if c.parent not in pdf_dirs]
    assert len(csvs) == 1, f"найдено {len(csvs)} CSV вне pdf-каталогов: {csvs}; все CSV: {all_csvs}"
    return {
        "root": root,
        "template": templates[0],
        "ledger_csv": csvs[0],
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


def load_ledger(wd: Path, input_dir: Path) -> dict:
    def build() -> dict[str, list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        dirty: list[dict[str, Any]] = []
        with open(find_inputs(input_dir)["ledger_csv"], newline="") as fh:
            for r in csv.DictReader(fh):
                rec = {
                    k: (r.get(k) or "").strip()
                    for k in ("txn_id", "date", "account_id", "counterparty", "description", "currency")
                }
                amt = parse_amount(r.get("amount") or "")
                if amt is None:
                    dirty.append({**rec, "raw_amount": r.get("amount")})
                    continue
                rec["amount"] = str(amt)
                # Первый ярус: категоризация по правилам
                rec["cat"] = categorize(rec["description"])
                rec["cat_tier"] = 1
                rows.append(rec)

        # Второй ярус: LLM для непокрытого
        other_descriptions = sorted({r["description"] for r in rows if r["cat"] == "OTHER"})
        if other_descriptions:
            llm_categories = categorize_batch(other_descriptions)
            for r in rows:
                if r["cat"] == "OTHER" and r["description"] in llm_categories:
                    r["cat"] = llm_categories[r["description"]]
                    r["cat_tier"] = 2

        rows.sort(key=lambda x: x["txn_id"])
        dirty.sort(key=lambda x: x["txn_id"])
        return {"rows": rows, "dirty": dirty}

    return artifact(wd / "ledger.json", LEDGER_VERSION, build)


def rows_of(art: dict) -> list[dict]:
    return [{**r, "amt": Decimal(r["amount"])} for r in art["rows"]]
