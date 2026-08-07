"""Ни одного имени заёмщика, порога или номера пункта вне tests/ и eval/."""

import json
from pathlib import Path

from grep_gate import _COVENANT_NUMBERS, _SCENARIOS, forbidden_literals, scan


def test_forbidden_list_is_substantial():
    """Forbidden list contains required categories (prefixes, related parties, thresholds)."""
    lits = forbidden_literals()
    assert "TXN-" in lits and "ACC-" in lits
    assert any("Ertis" in x for x in lits)


def test_scenarios_from_template():
    """Scenario IDs and covenant numbers loaded from submission template, not hardcoded."""
    template_path = Path("dataset/agentic-bank-public/submission_template.json")
    if template_path.exists():
        with open(template_path) as f:
            data = json.load(f)

        expected_scenarios = set(data["answers"].keys())
        expected_covenants = set()
        for scenario_data in data["answers"].values():
            expected_covenants.update(scenario_data.keys())

        assert _SCENARIOS == expected_scenarios, "Scenarios must come from template"
        assert _COVENANT_NUMBERS == expected_covenants, "Covenant numbers must come from template"


def test_planted_literal_caught(tmp_path):
    """Grep gate catches planted violations in any file."""
    bad = tmp_path / "bad.py"
    bad.write_text("threshold = 4_000_000  # P3 trigger\n")
    hits = scan([bad])
    assert hits, "Should catch planted literal"
    assert hits[0]["literal"] in {"4_000_000", "P3"}


def test_planted_violation_in_solution(tmp_path):
    """Solution files are scanned for violations (no allowlist exceptions)."""
    # Create fake solution dir in tmp_path
    solution_dir = tmp_path / "solution_test"
    solution_dir.mkdir()

    # Plant a violation: related party name
    test_file = solution_dir / "test_module.py"
    test_file.write_text('"""Module docstring.\n\nRelated party: Ertis.\n"""')

    hits = scan([test_file])
    assert hits, "Should catch planted Ertis name (related party from FACTS)"
    assert any(h["literal"] == "Ertis" for h in hits)


def test_solution_is_clean():
    """Current solution/ is free of knowledge leaks (after obfuscation fixes)."""
    files = sorted(Path("solution").glob("*.py")) + [Path("run.sh")]
    hits = scan(files)

    if hits:
        # Report violations for debugging
        lines = [f"{h['file']}:{h['line']}: {h['literal']}" for h in hits]
        msg = f"Found {len(hits)} violations:\n" + "\n".join(lines[:10])
        raise AssertionError(msg)

    assert not hits, "Solution should have no knowledge leaks"
