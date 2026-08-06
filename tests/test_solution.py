"""Регрессия по публичному набору: решение не должно молча деградировать.

Порог держим на текущем достигнутом результате. Улучшили решение — поднимите
BASELINE вместе с изменением, чтобы откат назад ловился сразу.
"""

import json

from solve import score, solve

BASELINE = 34.00
MAX_SCORE = 36.00
TEMPLATE = "dataset/agentic-bank-public/submission_template.json"
CLAUSES = ("6.1", "6.2", "6.3")


def test_score_not_below_baseline():
    total = score(solve())
    assert total >= BASELINE, f"скор упал: {total:.2f} < {BASELINE:.2f}"
    assert total <= MAX_SCORE


def test_answers_match_template_shape():
    expected = json.load(open(TEMPLATE))["answers"]
    answers = solve()

    assert sorted(answers) == sorted(expected), "набор сценариев разошёлся с шаблоном"
    for scenario, cells in answers.items():
        assert sorted(cells) == sorted(CLAUSES), f"{scenario}: не тот набор пунктов"
        for clause, cell in cells.items():
            where = f"{scenario} {clause}"
            assert sorted(cell) == sorted(expected[scenario][clause]), f"{where}: не те поля"
            assert cell["status"] in ("BREACH", "COMPLIANT"), f"{where}: статус {cell['status']!r}"
            assert isinstance(cell["actual"], float), f"{where}: actual не число"
            assert cell["evidence_txn_id"] is None or isinstance(cell["evidence_txn_id"], str)


def test_solve_is_deterministic():
    assert solve() == solve()
