import sys

sys.path.insert(0, "tools")

from score_private import load_key, score_private  # noqa: E402


def test_key_covers_84_cells():
    key = load_key()
    cells = sum(len(v["covenants"]) for v in key.values())
    assert cells == 84, cells


def test_perfect_answer_scores_full():
    key = load_key()
    answers = {sc: dict(v["covenants"]) for sc, v in key.items()}
    got = score_private(answers, key)
    assert got["cells"] == 84
    assert abs(got["total"] - 84.0) < 1e-9


def test_null_evidence_costs_only_where_key_has_id():
    key = load_key()
    answers = {
        sc: {cl: {**cell, "evidence_txn_id": None} for cl, cell in v["covenants"].items()}
        for sc, v in key.items()
    }
    got = score_private(answers, key)
    with_id = sum(
        1 for v in key.values() for cell in v["covenants"].values() if cell["evidence_txn_id"] is not None
    )
    assert abs((84.0 - got["total"]) - 0.20 * with_id) < 1e-9
