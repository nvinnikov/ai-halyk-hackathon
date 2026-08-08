"""Полный прогон на публичном архиве без эталонных фактов: агент сам читает PDF."""

import json
from pathlib import Path

import pytest

import solve
from score import score

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")
GT = json.loads(Path("dataset/agentic-bank-public/ground_truth.json").read_text())["scenarios"]


@pytest.mark.llm
def test_extracted_full_run_beats_floor():
    answers = solve.main(PUBLIC_ZIP, facts_source="extracted")
    total = score(answers, GT, verbose=True)
    # порог сознательно ниже 34.00: это первый честный прогон без подгонки;
    # разбор просадок — работа 8 августа (LOBO и extraction eval покажут где)
    assert total >= 30.00, f"извлечённый прогон просел: {total:.2f}"

    # Правка 6: скор можно набрать и приором — тест обязан ловить именно
    # работающее извлечение, а не удачную лестницу фолбэков.
    from ledger import extract_archive

    ds_hash, _ = extract_archive(PUBLIC_ZIP)
    trace_dir = Path("work") / ds_hash / "trace"
    cell_traces = [t for t in trace_dir.glob("*.json") if not t.stem.endswith(".borrower")]
    dsl_tier = sum(1 for t in cell_traces if json.loads(t.read_text()).get("tier") == 0)
    assert dsl_tier >= 30, f"ярусом dsl посчитано только {dsl_tier} из {len(cell_traces)} ячеек"


@pytest.mark.llm
def test_extracted_run_is_reproducible():
    a = solve.main(PUBLIC_ZIP, facts_source="extracted")
    b = solve.main(PUBLIC_ZIP, facts_source="extracted")
    assert a == b  # всё из кэша — детерминизм обязан держаться
