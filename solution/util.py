"""Общие утилиты: отпечаток датасета, стабильный JSON, денежная арифметика."""

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
OUT = ROOT / "out"


def dataset_hash(archive: Path) -> str:
    """Хеш содержимого входного архива — префикс всех производных артефактов."""
    return hashlib.sha256(archive.read_bytes()).hexdigest()[:16]


def workdir(ds_hash: str) -> Path:
    import util

    d = util.WORK / ds_hash
    d.mkdir(parents=True, exist_ok=True)
    return d


def stable_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1, default=str)


def q2(x: Decimal) -> float:
    """actual с двумя знаками; ROUND_HALF_UP, а не банковское round()."""
    return float(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
