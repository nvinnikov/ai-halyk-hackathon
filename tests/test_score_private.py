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


# Синтетические ячейки ниже проверяют разложение на компоненты при частичном
# scale (0 < scale < 1) — ветку, которую предыдущие три теста не исполняют
# (там actual всегда совпадает с ключом, e == 0, scale == 1.0). Отклонение
# actual на 2% от ключевого даёт e = 0.02, scale = 1 - 0.02/0.05 = 0.6.
# Ожидания — числа, посчитанные руками по формуле CASE.ru.md, а не тем же
# кодом, что и score_private, — иначе тест ничего не проверяет.


def test_partial_scale_components_with_evidence_id():
    key = {
        "S": {
            "covenants": {"6.1": {"status": "BREACH", "actual": 1000000.0, "evidence_txn_id": "TXN-S-0001"}}
        }
    }
    answers = {"S": {"6.1": {"status": "BREACH", "actual": 1020000.0, "evidence_txn_id": "TXN-S-0001"}}}
    got = score_private(answers, key)
    assert got["status_pts"] == 0.50
    assert abs(got["actual_pts"] - 0.18) < 1e-9
    assert got["evidence_pts"] == 0.20
    assert abs(got["total"] - 0.88) < 1e-9


def test_partial_scale_components_with_null_evidence():
    key = {"S": {"covenants": {"6.1": {"status": "COMPLIANT", "actual": 1000000.0, "evidence_txn_id": None}}}}
    answers = {"S": {"6.1": {"status": "COMPLIANT", "actual": 1020000.0, "evidence_txn_id": None}}}
    got = score_private(answers, key)
    assert got["status_pts"] == 0.50
    assert abs(got["actual_pts"] - 0.18) < 1e-9
    assert abs(got["evidence_pts"] - 0.12) < 1e-9
    assert abs(got["total"] - 0.80) < 1e-9


def test_partial_scale_wrong_evidence_scores_zero_evidence_regardless_of_scale():
    key = {
        "S": {
            "covenants": {"6.1": {"status": "BREACH", "actual": 1000000.0, "evidence_txn_id": "TXN-S-0001"}}
        }
    }
    answers = {"S": {"6.1": {"status": "BREACH", "actual": 1020000.0, "evidence_txn_id": "TXN-WRONG"}}}
    got = score_private(answers, key)
    assert got["status_pts"] == 0.50
    assert abs(got["actual_pts"] - 0.18) < 1e-9
    assert got["evidence_pts"] == 0.0
    assert abs(got["total"] - 0.68) < 1e-9
