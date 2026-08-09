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


def test_find_inputs_multiple_csv_in_root_picks_ledger(tmp_path, monkeypatch):
    """Два CSV в root больше НЕ ошибка (ревью перед окном).

    Раньше здесь был AssertionError, и он обнулял бы весь прогон: find_inputs
    зовётся до записи скелета submission, а run.sh идёт под set -e. Лишний CSV в
    корне приватного пакета (справочник курсов, лог выгрузки) — сценарий не
    экзотический: в публичном пакете второй CSV уже лежит, просто в подкаталоге.
    Теперь леджер выбирается по заголовку, а факт выбора виден алярмом.
    """
    import util

    monkeypatch.setattr(util, "WORK", tmp_path)

    archive = tmp_path / "test_multi_csv.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("dataset/submission_template.json", "{}")
        z.writestr("dataset/ledger.csv", _make_csv_content())
        z.writestr("dataset/other.csv", "a,b\n1,2\n")

    ds_hash, input_dir = extract_archive(archive)
    assert find_inputs(input_dir)["ledger_csv"].name == "ledger.csv"


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


def test_categorize_failure_not_cached(monkeypatch, tmp_path):
    """categorize_failed не запекается в ledger.json: расход целевого
    заёмщика не должен залипнуть в OTHER навсегда — перезапуск после
    устранения причины перекатегоризирует (ревью PR #9, 23-я волна)."""
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
                "amount": "-10.00",
            }
        )
    wd = tmp_path / "wd"
    failing = lambda descs: ({}, [{"kind": "categorize_failed", "batch_start": descs[0], "error": "x"}])  # noqa: E731
    monkeypatch.setattr(ledger_mod, "categorize_batch", failing)
    art = ledger_mod.load_ledger(wd, root, target_scenarios=["S1"])
    assert any(a["kind"] == "categorize_failed" for a in art["alarms"])
    assert not (wd / "ledger.json").exists()

    monkeypatch.setattr(ledger_mod, "categorize_batch", lambda descs: ({d: "CONSULTING" for d in descs}, []))
    art2 = ledger_mod.load_ledger(wd, root, target_scenarios=["S1"])
    assert art2["rows"][0]["cat"] == "CONSULTING"
    assert (wd / "ledger.json").exists()


# --- выбор леджера среди нескольких CSV (ревью перед окном) --------------------


def _pkg(tmp_path, *csvs: tuple[str, str]):
    """Пакет датасета: шаблон, один PDF и заданные CSV в корне."""
    root = tmp_path / "pkg"
    (root / "documents").mkdir(parents=True)
    (root / "submission_template.json").write_text('{"answers": {}}')
    (root / "documents" / "a.pdf").write_bytes(b"%PDF-1.4\n")
    for name, header in csvs:
        (root / name).write_text(header + "\n1,2,3\n")
    return tmp_path


def test_extra_csv_in_root_does_not_raise(tmp_path):
    """Ассерт здесь стоил бы ВСЕГО прогона: find_inputs зовётся раньше записи
    скелета, а run.sh идёт под set -e. Лишний CSV в корне приватного пакета —
    справочник курсов, лог выгрузки — обнулял бы окно."""
    root = _pkg(
        tmp_path,
        ("fx_reference.csv", "currency,rate,note"),
        ("master_ledger_2025.csv", "txn_id,date,account_id,counterparty,description,amount,currency"),
    )
    got = find_inputs(root)
    assert got["ledger_csv"].name == "master_ledger_2025.csv"


def test_ledger_picked_by_header_not_by_alphabet(tmp_path):
    """Алфавит случаен и поставил бы справочник первым; заголовок — свойство
    формата задания."""
    root = _pkg(
        tmp_path,
        ("aaa_first_by_alphabet.csv", "currency,rate,note"),
        ("zzz_last.csv", "txn_id,amount,account_id"),
    )
    assert find_inputs(root)["ledger_csv"].name == "zzz_last.csv"


def test_ledger_ties_broken_by_size(tmp_path):
    """Одинаковые заголовки — берём крупнейший: у леджера строк больше, чем у
    выдержки из него."""
    root = tmp_path / "pkg"
    (root / "documents").mkdir(parents=True)
    (root / "submission_template.json").write_text('{"answers": {}}')
    (root / "documents" / "a.pdf").write_bytes(b"%PDF-1.4\n")
    head = "txn_id,amount,account_id"
    (root / "sample.csv").write_text(head + "\n1,2,3\n")
    (root / "full.csv").write_text(head + "\n" + "1,2,3\n" * 500)
    assert find_inputs(root)["ledger_csv"].name == "full.csv"


def test_csv_only_outside_root_still_found(tmp_path):
    """Прежний фолбэк на rglob сохранён: CSV может лежать не в корне."""
    root = tmp_path / "pkg"
    (root / "documents").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "submission_template.json").write_text('{"answers": {}}')
    (root / "documents" / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (root / "data" / "ledger.csv").write_text("txn_id,amount,account_id\n1,2,3\n")
    assert find_inputs(root)["ledger_csv"].name == "ledger.csv"
