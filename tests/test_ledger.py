"""Разбор amount не имеет права уронить прогон; строки маршрутизируются по account_id."""

import csv
import io
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from ledger import extract_archive, find_inputs, load_ledger, parse_amount, rows_of

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")


def _make_csv_content() -> str:
    """Минимальный CSV для тестов: 2 валидные строки."""
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["txn_id", "date", "account_id", "counterparty", "description", "amount", "currency"],
    )
    writer.writeheader()
    writer.writerow(
        {
            "txn_id": "TXN-001",
            "date": "2025-01-01",
            "account_id": "ACC-001",
            "counterparty": "Test Corp",
            "description": "Test transaction",
            "amount": "100.00",
            "currency": "USD",
        }
    )
    writer.writerow(
        {
            "txn_id": "TXN-002",
            "date": "2025-01-02",
            "account_id": "ACC-001",
            "counterparty": "Test Corp 2",
            "description": "Another transaction",
            "amount": "200.50",
            "currency": "USD",
        }
    )
    return output.getvalue()


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


def test_find_inputs_fallback_nested_csv(tmp_path, monkeypatch):
    """Фолбэк: ledger CSV во вложенной папке (не в root), без PDF там же."""
    import util

    monkeypatch.setattr(util, "WORK", tmp_path)

    # Создаём архив: template в root, ledger в подпапке
    archive = tmp_path / "test_fallback.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("dataset/submission_template.json", "{}")
        z.writestr("dataset/data/ledger.csv", _make_csv_content())

    ds_hash, input_dir = extract_archive(archive)
    inputs = find_inputs(input_dir)
    assert inputs["template"].name == "submission_template.json"
    assert inputs["ledger_csv"].name == "ledger.csv"
    # CSV найден в подпапке через фолбэк
    assert "data" in str(inputs["ledger_csv"])


def test_find_inputs_error_multiple_csv_in_root(tmp_path, monkeypatch):
    """Ошибка: два CSV в root → AssertionError с перечнем."""
    import util

    monkeypatch.setattr(util, "WORK", tmp_path)

    archive = tmp_path / "test_multi_csv.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("dataset/submission_template.json", "{}")
        z.writestr("dataset/ledger.csv", _make_csv_content())
        z.writestr("dataset/other.csv", "a,b\n1,2\n")

    ds_hash, input_dir = extract_archive(archive)
    with pytest.raises(AssertionError, match="в root найдено 2 CSV"):
        find_inputs(input_dir)


def test_dirty_rows_get_second_tier(monkeypatch, tmp_path):
    """Строка с грязной суммой, оживающая через amount_override, не должна
    оставаться OTHER: второй ярус обязан видеть и dirty."""
    import csv as _csv

    import ledger as ledger_mod

    root = tmp_path / "input"
    root.mkdir()
    (root / "submission_template.json").write_text('{"answers": {}}')
    with open(root / "ledger.csv", "w", newline="") as fh:
        w = _csv.DictWriter(
            fh,
            fieldnames=["txn_id", "date", "account_id", "counterparty", "description", "currency", "amount"],
        )
        w.writeheader()
        w.writerow(
            {
                "txn_id": "TXN-S1-0001",
                "date": "2025-01-01",
                "account_id": "A-1",
                "counterparty": "X",
                "description": "Mystery payment",
                "currency": "USD",
                "amount": "n/a",
            }
        )
    monkeypatch.setattr(ledger_mod, "categorize_batch", lambda descs: ({d: "CONSULTING" for d in descs}, []))
    art = ledger_mod.load_ledger(tmp_path, root, target_scenarios=["S1"])
    assert art["dirty"][0]["cat"] == "CONSULTING"
    assert art["dirty"][0]["cat_tier"] == 2
