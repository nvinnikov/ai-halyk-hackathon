"""Разбор amount не имеет права уронить прогон; строки маршрутизируются по account_id."""

from decimal import Decimal
from pathlib import Path

import pytest

from ledger import extract_archive, find_inputs, load_ledger, parse_amount, rows_of

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("-366837.86", Decimal("-366837.86")),
        ("1,234.56", Decimal("1234.56")),
        ("(500.00)", Decimal("-500.00")),
        ("n/a", None),
        ("", None),
        ("  ", None),
        ("—", None),
        ("garbage", None),
    ],
)
def test_parse_amount(raw, want):
    assert parse_amount(raw) == want


def test_extract_and_load_public(tmp_path, monkeypatch):
    import util

    monkeypatch.setattr(util, "WORK", tmp_path)
    ds_hash, input_dir = extract_archive(PUBLIC_ZIP)
    assert len(ds_hash) == 16
    inputs = find_inputs(input_dir)
    assert inputs["template"].name == "submission_template.json"
    assert inputs["ledger_csv"].suffix == ".csv"
    assert len(inputs["pdfs"]) > 10

    art = load_ledger(tmp_path / ds_hash, input_dir)
    rows = rows_of(art)
    assert len(rows) + len(art["dirty"]) == 1473
    assert rows == sorted(rows, key=lambda r: r["txn_id"])
    assert all(isinstance(r["amt"], Decimal) for r in rows)
    assert all(r["account_id"] for r in rows)

    # идемпотентность: повторная загрузка отдаёт готовый артефакт
    assert load_ledger(tmp_path / ds_hash, input_dir)["rows"] == art["rows"]
