"""Формула из CASE.ru.md раздел 4: status 0.50, actual 0.30 по шкале,
evidence 0.20 (при null в ключе — по той же шкале, что и actual)."""

import pytest

from score import score


def gt_cell(status, actual, ev):
    return {"status": status, "actual": actual, "evidence_txn_id": ev}


def wrap(cell, gt):
    return {"S1": {"6.1": cell}}, {"S1": {"covenants": {"6.1": gt}}}


def test_exact_match_with_evidence():
    a, g = wrap(gt_cell("BREACH", 100.0, "TXN-S1-1"), gt_cell("BREACH", 100.0, "TXN-S1-1"))
    assert score(a, g, verbose=False) == pytest.approx(1.0)


def test_wrong_status_zeroes_cell():
    a, g = wrap(gt_cell("COMPLIANT", 100.0, None), gt_cell("BREACH", 100.0, None))
    assert score(a, g, verbose=False) == 0.0


def test_actual_error_scales_both_components_when_null_key():
    # ошибка 2.5% — половина и от 0.30, и от 0.20
    a, g = wrap(gt_cell("BREACH", 102.5, None), gt_cell("BREACH", 100.0, None))
    assert score(a, g, verbose=False) == pytest.approx(0.5 + 0.15 + 0.10)


def test_wrong_evidence_with_nonnull_key():
    a, g = wrap(gt_cell("BREACH", 100.0, "TXN-S1-2"), gt_cell("BREACH", 100.0, "TXN-S1-1"))
    assert score(a, g, verbose=False) == pytest.approx(0.8)


def test_nonnumeric_actual_keeps_status_points():
    a, g = wrap(gt_cell("BREACH", None, None), gt_cell("BREACH", 100.0, None))
    assert score(a, g, verbose=False) == pytest.approx(0.5)


def test_prints_evidence(capsys):
    a, g = wrap(gt_cell("BREACH", 100.0, "TXN-S1-1"), gt_cell("BREACH", 100.0, "TXN-S1-9"))
    score(a, g, verbose=True)
    out = capsys.readouterr().out
    assert "TXN-S1-1" in out and "TXN-S1-9" in out
