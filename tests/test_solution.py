"""Гейт фундамента: run.sh на публичном архиве воспроизводит 34.00,
submission валиден на любой секунде прогона, ячейка падает — прогон нет.

Все вызовы solve.main здесь передают facts_source="expected" явно: задача 24
сменит дефолт на "extracted", и неявные вызовы молча стали бы боевыми
LLM-прогонами.
"""

import json
from pathlib import Path

import pytest

import solve
from score import score

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")
GT = json.loads(Path("dataset/agentic-bank-public/ground_truth.json").read_text())["scenarios"]
TEMPLATE = json.loads(Path("dataset/agentic-bank-public/submission_template.json").read_text())
BASELINE = 34.00
CELL_FIELDS = ("actual", "evidence_txn_id", "status")


def assert_cell_valid(cell: dict, where: str) -> None:
    """Форма ячейки. dump_submission сверяет с шаблоном только пары
    (сценарий, пункт), поля внутри ячейки не проверяет никто, кроме этого."""
    assert sorted(cell) == list(CELL_FIELDS), f"{where}: поля {sorted(cell)}"
    assert cell["status"] in ("BREACH", "COMPLIANT"), f"{where}: статус {cell['status']!r}"
    assert isinstance(cell["actual"], int | float) and not isinstance(cell["actual"], bool), (
        f"{where}: actual не число ({cell['actual']!r})"
    )
    assert cell["evidence_txn_id"] is None or isinstance(cell["evidence_txn_id"], str), (
        f"{where}: улика не строка и не None ({cell['evidence_txn_id']!r})"
    )


@pytest.fixture(scope="module")
def answers():
    return solve.main(PUBLIC_ZIP, facts_source="expected")


def test_score_not_below_baseline(answers):
    total = score(answers, GT, verbose=True)
    assert total >= BASELINE, f"скор упал: {total:.2f} < {BASELINE:.2f}"


def test_hash_printed_first(capsys):
    solve.main(PUBLIC_ZIP, facts_source="expected")
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("dataset_hash: ")


def test_template_cells_have_expected_fields():
    """CELL_FIELDS — не выдумка теста, а форма ячейки из шаблона организаторов."""
    for sc, cells in TEMPLATE["answers"].items():
        for clause, cell in cells.items():
            assert sorted(cell) == list(CELL_FIELDS), f"шаблон {sc} {clause}: поля {sorted(cell)}"


def test_submission_file_matches_template(answers):
    sub = json.loads(Path("out/submission.json").read_text())
    assert sorted(sub["answers"]) == sorted(TEMPLATE["answers"])
    for sc, cells in sub["answers"].items():
        assert sorted(cells) == sorted(TEMPLATE["answers"][sc])
        for clause, cell in cells.items():
            assert_cell_valid(cell, f"{sc} {clause}")


def test_cell_failure_does_not_kill_run(monkeypatch):
    original = solve.solve_cell
    victim = sorted(TEMPLATE["answers"])[0]

    def sabotaged(scenario, clause, rows, facts, raw):
        if scenario == victim:
            raise RuntimeError("искусственный сбой ячейки")
        return original(scenario, clause, rows, facts, raw)

    monkeypatch.setattr(solve, "solve_cell", sabotaged)
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    for clause, cell in answers[victim].items():
        assert_cell_valid(cell, f"{victim} {clause}")


def test_scenario_load_failure_does_not_kill_run(monkeypatch):
    """Падение загрузки сценария не убивает прогон: его три ячейки остаются
    скелетом, остальные сценарии считаются."""
    victim = sorted(TEMPLATE["answers"])[0]
    original = solve.load_rows

    def sabotaged(scenario, all_rows, index, facts, donor_rates):
        if scenario == victim:
            raise RuntimeError("искусственный сбой загрузки сценария")
        return original(scenario, all_rows, index, facts, donor_rates)

    monkeypatch.setattr(solve, "load_rows", sabotaged)
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    for clause, cell in answers[victim].items():
        assert_cell_valid(cell, f"{victim} {clause}")
    other = sorted(TEMPLATE["answers"])[1]
    assert any(cell["evidence_txn_id"] is not None for cell in answers[other].values())


def test_trace_written_per_cell(answers):
    from ledger import extract_archive

    ds_hash, _ = extract_archive(PUBLIC_ZIP)
    traces = list((Path("work") / ds_hash / "trace").glob("*.json"))
    assert len(traces) == 36


def test_deterministic(answers):
    assert answers == solve.main(PUBLIC_ZIP, facts_source="expected")
