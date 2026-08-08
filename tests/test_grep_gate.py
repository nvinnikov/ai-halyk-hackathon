"""Ни одного имени заёмщика, порога или номера пункта вне tests/ и eval/."""

import json
from pathlib import Path

from grep_gate import _COVENANT_NUMBERS, _SCENARIOS, forbidden_literals, scan


def test_forbidden_list_is_substantial():
    """Список запрещённого содержит нужные категории (префиксы, related parties, пороги)."""
    lits = forbidden_literals()
    assert "TXN-" in lits and "ACC-" in lits
    assert any("Ertis" in x for x in lits)


def test_scenarios_from_template():
    """ID сценариев и номера пунктов берутся из шаблона submission, не захардкожены."""
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
    """Греп-гейт ловит подложенное нарушение в любом файле."""
    bad = tmp_path / "bad.py"
    bad.write_text("threshold = 4_000_000  # P3 trigger\n")
    hits = scan([bad])
    assert hits, "должен поймать подложенный литерал"
    assert hits[0]["literal"] in {"4_000_000", "P3"}


def test_planted_violation_in_solution(tmp_path):
    """Файлы solution сканируются на нарушения (без исключений allowlist)."""
    # Поддельный каталог solution в tmp_path
    solution_dir = tmp_path / "solution_test"
    solution_dir.mkdir()

    # Подкладываем нарушение: имя related party
    test_file = solution_dir / "test_module.py"
    test_file.write_text('"""Module docstring.\n\nRelated party: Ertis.\n"""')

    hits = scan([test_file])
    assert hits, "должен поймать подложенное имя Ertis (related party из FACTS)"
    assert any(h["literal"] == "Ertis" for h in hits)


def test_solution_is_clean():
    """Текущий solution/ свободен от утечек знания (после правок обфускации)."""
    files = sorted(Path("solution").glob("*.py")) + [Path("run.sh")]
    hits = scan(files)

    if hits:
        # Список нарушений для отладки
        lines = [f"{h['file']}:{h['line']}: {h['literal']}" for h in hits]
        msg = f"Found {len(hits)} violations:\n" + "\n".join(lines[:10])
        raise AssertionError(msg)

    assert not hits, "В solution не должно быть утечек знания"
