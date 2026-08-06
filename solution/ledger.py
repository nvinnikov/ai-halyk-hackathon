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


LEDGER_VERSION = 1
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
    """Файлы датасета ищутся, а не зашиваются именами (раздел 9)."""
    templates = sorted(input_dir.rglob("submission_template.json"))
    assert len(templates) == 1, f"шаблонов найдено {len(templates)}"
    root = templates[0].parent
    # Ищем CSV файлы только в корневой папке, исключая подпапки
    csvs = sorted(root.glob("*.csv"))
    assert len(csvs) >= 1, f"csv в корне найдено {len(csvs)}: {csvs}"
    return {
        "root": root,
        "template": templates[0],
        "ledger_csv": csvs[0],
        "pdfs": sorted(root.rglob("*.pdf")),
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
                rec["cat"] = categorize(rec["description"])
                rows.append(rec)
        rows.sort(key=lambda x: x["txn_id"])
        dirty.sort(key=lambda x: x["txn_id"])
        return {"rows": rows, "dirty": dirty}

    return artifact(wd / "ledger.json", LEDGER_VERSION, build)


def rows_of(art: dict) -> list[dict]:
    return [{**r, "amt": Decimal(r["amount"])} for r in art["rows"]]
