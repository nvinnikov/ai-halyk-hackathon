"""Целевое множество задаёт шаблон; фон — не ошибка, а число в отчёте."""

from scindex import build_index


def row(txn, acc):
    return {"txn_id": txn, "account_id": acc}


def test_happy_path_and_background():
    rows = [
        row("TXN-S1-0001", "ACC-1"),
        row("TXN-S1-0002", "ACC-1"),
        row("TXN-S2-0001", "ACC-2"),
        row("TXN-9001-0001", "ACC-9001"),  # фоновый счёт
        row("TXN-9001-0002", "ACC-9001"),
    ]
    idx = build_index(rows, ["S1", "S2"])
    assert idx["scenario_to_account"] == {"S1": "ACC-1", "S2": "ACC-2"}
    assert idx["account_to_scenario"] == {"ACC-1": "S1", "ACC-2": "S2"}
    assert idx["background"] == {"accounts": 1, "rows": 2, "row_share": 0.4}
    assert idx["alarms"] == []


def test_pattern_not_positional():
    # scenario_id не обязан стоять вторым компонентом
    idx = build_index([row("OP-2025-S7-99", "ACC-9")], ["S7"])
    assert idx["scenario_to_account"] == {"S7": "ACC-9"}


def test_pattern_underscore_separator():
    # разделитель может быть подчёркиванием, а не дефисом
    idx = build_index([row("TXN_S7_0001", "ACC-9")], ["S7"])
    assert idx["scenario_to_account"] == {"S7": "ACC-9"}


def test_pattern_no_separator():
    # scenario_id как часть слова должен найтись
    idx = build_index([row("S7-0001", "ACC-9")], ["S7"])
    assert idx["scenario_to_account"] == {"S7": "ACC-9"}


def test_zero_accounts_is_alarm():
    idx = build_index([row("TXN-9001-0001", "ACC-9001")], ["S1"])
    assert idx["scenario_to_account"] == {}
    assert idx["alarms"][0]["kind"] == "index_cardinality"


def test_two_accounts_is_alarm():
    rows = [row("TXN-S1-0001", "ACC-1"), row("TXN-S1-0002", "ACC-2")]
    idx = build_index(rows, ["S1"])
    assert "S1" not in idx["scenario_to_account"]
    assert idx["alarms"][0]["accounts"] == ["ACC-1", "ACC-2"]


def test_shared_account_within_targets_is_alarm():
    rows = [row("TXN-S1-0001", "ACC-1"), row("TXN-S2-0001", "ACC-1")]
    idx = build_index(rows, ["S1", "S2"])
    kinds = {a["kind"] for a in idx["alarms"]}
    assert "shared_account" in kinds


def test_ambiguous_txn_alarm():
    # одна строка содержит несколько целевых id — алярм
    rows = [row("TXN-S1-S2-0001", "ACC-X")]
    idx = build_index(rows, ["S1", "S2"])
    assert idx["scenario_to_account"] == {}
    alarm_kinds = {a["kind"] for a in idx["alarms"]}
    assert "ambiguous_txn" in alarm_kinds
    ambig = [a for a in idx["alarms"] if a["kind"] == "ambiguous_txn"][0]
    assert ambig["txn_id"] == "TXN-S1-S2-0001"


def test_regex_boundary_p1_not_in_p10():
    # P1 не должен матчиться в P10 — критичный negative test
    idx = build_index([row("TXN-P10-0001", "ACC-1")], ["P1"])
    assert idx["scenario_to_account"] == {}
    assert idx["background"]["rows"] == 1
    # P1 не найден, индекс пуст, алярм на 0 счётов
    assert any(a["kind"] == "index_cardinality" for a in idx["alarms"])


def test_regex_boundary_s7_not_in_s7x():
    # S7 не должен матчиться в S7X — критичный negative test
    idx = build_index([row("TXN-S7X-0001", "ACC-1")], ["S7"])
    assert idx["scenario_to_account"] == {}
    assert idx["background"]["rows"] == 1


def test_regex_boundary_p10_without_ambiguous():
    # Позитивный контроль: при P1 и P10 строка TXN-P10-0001 матчится ровно в P10
    # (не ambiguous, не в P1)
    idx = build_index([row("TXN-P10-0001", "ACC-1")], ["P1", "P10"])
    assert idx["scenario_to_account"] == {"P10": "ACC-1"}
    assert "P1" not in idx["scenario_to_account"]
    # Нет ambiguous_txn — P10 матчился один раз
    alarm_kinds = {a["kind"] for a in idx["alarms"]}
    assert "ambiguous_txn" not in alarm_kinds


def test_public_dataset_matches_spec_numbers(tmp_path, monkeypatch):
    import json

    import util
    from ledger import extract_archive, find_inputs, load_ledger, rows_of

    monkeypatch.setattr(util, "WORK", tmp_path)
    ds_hash, input_dir = extract_archive(__import__("pathlib").Path("6a741640c31eb032062683.zip"))
    rows = rows_of(load_ledger(tmp_path / ds_hash, input_dir))
    targets = sorted(json.load(open(find_inputs(input_dir)["template"]))["answers"])
    idx = build_index(rows, targets)
    assert len(idx["scenario_to_account"]) == 12
    assert idx["alarms"] == []
    assert idx["background"]["accounts"] == 549
    assert idx["background"]["rows"] == 800
