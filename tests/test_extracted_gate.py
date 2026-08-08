"""Офлайн-гейт БОЕВОГО (extracted) пути — ревью PR #9, 4-я волна.

BASELINE в test_solution.py меряет expected-режим, которого 9 августа не
существует; llm-floor-тест отсечён маркером. Этот тест закрывает дыру: на
прогретом чекауте (артефакты документного конвейера на диске, LLM_OFFLINE=1
гарантирует ноль сетевых вызовов) extracted-прогон обязан держать свой
регрессионный порог. На холодном CI артефактов нет — честный skip, как у
тестов мутаций.

Улучшили извлечение/сшивку — поднимите порог тем же коммитом.
"""

import json
from pathlib import Path

import pytest

import solve
from score import score

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")
EXTRACTED_BASELINE = 29.5


@pytest.fixture(scope="module")
def warmed_workdir(monkeypatch_module=None):
    from ledger import extract_archive
    from util import workdir

    ds_hash, _ = extract_archive(PUBLIC_ZIP)
    wd = workdir(ds_hash)
    if not (wd / "facts").is_dir() or not (wd / "specs").is_dir():
        pytest.skip("публичный workdir не прогрет (артефакты документного конвейера отсутствуют)")
    return wd


def test_extracted_offline_score_not_below_baseline(warmed_workdir, monkeypatch):
    monkeypatch.setenv("LLM_OFFLINE", "1")  # ноль сетевых вызовов: только артефакты и кэш
    answers = solve.main(PUBLIC_ZIP, facts_source="extracted")
    gt = json.loads(Path("dataset/agentic-bank-public/ground_truth.json").read_text())["scenarios"]
    total = score(answers, gt)
    assert total >= EXTRACTED_BASELINE, f"extracted-прогон просел: {total:.2f} < {EXTRACTED_BASELINE}"
