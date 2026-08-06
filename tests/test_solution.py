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
# Потолок фундамента по LOBO-замеру: скор выше — признак подгонки под публичный
# ключ, а не улучшения. Легитимный рост потолка поднимается осознанно, тем же
# коммитом, что и его причина.
MAX_SCORE = 34.00
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
    assert total <= MAX_SCORE + 1e-9, f"скор выше потолка: {total:.2f} > {MAX_SCORE:.2f} — подгонка?"


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
    """Сломанное вычисление не убивает прогон: ячейка приходит по лестнице,
    и прочитанный порог не выбрасывается (5.7) — actual равен порогу спеки,
    а не медиане и не 1.0."""
    from decimal import Decimal

    from expected_extraction import SPECS

    import evidence

    def sabotaged(raw, facts, cellspec, overrides=None, set_exclude=frozenset()):
        raise RuntimeError("искусственный сбой вычисления")

    monkeypatch.setattr(evidence, "compute", sabotaged)
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    for sc, cells in answers.items():
        for clause, cell in cells.items():
            assert_cell_valid(cell, f"{sc} {clause}")
            assert cell["actual"] == float(Decimal(str(SPECS[sc][clause][2])))


def test_diagnostics_failure_does_not_kill_run(monkeypatch):
    """Диагностика (borrower-трейс, sign_divergence) — не расчёт: её падение
    не должно ни убивать прогон, ни отбрасывать уже посчитанную ячейку.
    После задачи 24 категории приходят от LLM, и expand() внутри
    sign_divergence может бросить KeyError на первой же невалидной."""

    def boom(*a, **k):
        raise KeyError("категория вне таксономии")

    monkeypatch.setattr(solve, "sign_divergence", boom)
    monkeypatch.setattr(solve, "_write_borrower_trace", boom)
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    total = score(answers, GT, verbose=False)
    assert total >= BASELINE  # ячейки посчитаны, диагностика потеряна — не наоборот


def test_unknown_scenario_facts_do_not_kill_run(monkeypatch):
    """Сценарий без фактов в эталоне (приватный набор): расчёт идёт по строкам
    без документальных решений, остальные сценарии не страдают."""
    victim = sorted(TEMPLATE["answers"])[0]
    monkeypatch.delitem(solve.FACTS, victim)
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    for clause, cell in answers[victim].items():
        assert_cell_valid(cell, f"{victim} {clause}")
    other = sorted(TEMPLATE["answers"])[1]
    assert any(cell["evidence_txn_id"] is not None for cell in answers[other].values())


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
    borrower = [t for t in traces if t.stem.endswith(".borrower")]
    assert len(traces) - len(borrower) == 36
    assert len(borrower) == 12


def test_deterministic(answers):
    assert answers == solve.main(PUBLIC_ZIP, facts_source="expected")


def _cell_traces() -> list[Path]:
    """Трейсы ячеек публичного прогона. Каталог адресуется отпечатком архива,
    иначе сюда попали бы трейсы мутированного прогона (задача 4)."""
    from util import dataset_hash, workdir

    return sorted((workdir(dataset_hash(PUBLIC_ZIP)) / "trace").glob("*.*.json"))


def test_other_unassigned_absent_on_public_set(answers):
    """На публичном наборе OTHER пуст у всех целевых — алярма быть не должно.

    Тест держит границу: срабатывание здесь означает, что категоризация
    поехала, а не что алярм неверен."""
    traces = _cell_traces()
    assert traces, "трейсы ячеек не найдены — прогон не состоялся"
    with_alarm = [t.name for t in traces if json.loads(t.read_text()).get("other_unassigned") is not None]
    assert with_alarm == [], f"неожиданный other_unassigned: {with_alarm}"


def test_other_unassigned_written_when_rows_lost(monkeypatch):
    """Строка, ушедшая в OTHER, обязана поднять алярм в трейсе ячейки."""
    original = solve.load_rows

    def lossy(scenario, all_rows, index, facts, donor_rates):
        raw, rows, alarms = original(scenario, all_rows, index, facts, donor_rates)
        # Первая строка выручки «не опозналась»: ровно тот промах, ради
        # которого алярм и вводится.
        for r in rows:
            if r["cat"] == "REVENUE":
                r["cat"] = "OTHER"
                break
        return raw, rows, alarms

    monkeypatch.setattr(solve, "load_rows", lossy)
    try:
        solve.main(PUBLIC_ZIP, facts_source="expected")
        hit = [
            a
            for a in (json.loads(t.read_text()).get("other_unassigned") for t in _cell_traces())
            if a is not None
        ]
        assert hit, "потерянная строка REVENUE не подняла ни одного алярма"
        assert all(a["other_sum"] != "0" for a in hit)
    finally:
        # Трейсы на диске общие: испорченный прогон обязан быть переписан
        # чистым, иначе соседний тест увидит чужой алярм.
        monkeypatch.undo()
        solve.main(PUBLIC_ZIP, facts_source="expected")
