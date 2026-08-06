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


def test_submission_file_matches_template(answers):
    sub = json.loads(Path("out/submission.json").read_text())
    assert sorted(sub["answers"]) == sorted(TEMPLATE["answers"])
    for sc, cells in sub["answers"].items():
        assert sorted(cells) == sorted(TEMPLATE["answers"][sc])
        for cell in cells.values():
            assert cell["status"] in ("BREACH", "COMPLIANT")
            assert isinstance(cell["actual"], int | float)


def test_cell_failure_does_not_kill_run(monkeypatch):
    original = solve.solve_cell
    victim = sorted(TEMPLATE["answers"])[0]

    def sabotaged(scenario, clause, rows, facts):
        if scenario == victim:
            raise RuntimeError("искусственный сбой ячейки")
        return original(scenario, clause, rows, facts)

    monkeypatch.setattr(solve, "solve_cell", sabotaged)
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    for cell in answers[victim].values():
        assert cell["status"] in ("BREACH", "COMPLIANT")
        assert isinstance(cell["actual"], int | float)


def test_scenario_load_failure_does_not_kill_run(monkeypatch):
    """Падение загрузки сценария не убивает прогон: его три ячейки остаются
    скелетом, остальные сценарии считаются."""
    victim = sorted(TEMPLATE["answers"])[0]
    original = solve.load

    def sabotaged(scenario):
        if scenario == victim:
            raise RuntimeError("искусственный сбой загрузки сценария")
        return original(scenario)

    monkeypatch.setattr(solve, "load", sabotaged)
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    for cell in answers[victim].values():
        assert cell["status"] in ("BREACH", "COMPLIANT")
        assert isinstance(cell["actual"], int | float)
    other = sorted(TEMPLATE["answers"])[1]
    assert any(cell["evidence_txn_id"] is not None for cell in answers[other].values())


def test_trace_written_per_cell(answers):
    from ledger import extract_archive

    ds_hash, _ = extract_archive(PUBLIC_ZIP)
    traces = list((Path("work") / ds_hash / "trace").glob("*.json"))
    assert len(traces) == 36


def test_deterministic(answers):
    assert answers == solve.main(PUBLIC_ZIP, facts_source="expected")
