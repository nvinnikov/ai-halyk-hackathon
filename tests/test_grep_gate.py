"""Ни одного имени заёмщика, порога или номера пункта вне tests/ и eval/."""

from pathlib import Path

from grep_gate import forbidden_literals, scan


def test_forbidden_list_is_substantial():
    lits = forbidden_literals()
    assert "TXN-" in lits and "ACC-" in lits
    assert any("Ertis" in x for x in lits)


def test_planted_literal_caught(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("threshold = 4_000_000  # P3 trigger\n")
    hits = scan([bad])
    assert hits and hits[0]["literal"] in {"4_000_000", "P3"}


def test_solution_is_clean():
    files = sorted(Path("solution").glob("*.py")) + [Path("run.sh")]
    assert scan(files) == []
