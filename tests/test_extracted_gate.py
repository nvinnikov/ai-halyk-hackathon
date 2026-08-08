"""Офлайн-гейт БОЕВОГО (extracted) пути — ревью PR #9, 4-я волна.

BASELINE в test_solution.py меряет expected-режим, которого 9 августа не
существует; llm-floor-тест отсечён маркером. Этот тест закрывает дыру:
LLM_OFFLINE=1 гарантирует ноль сетевых вызовов, а extracted-прогон обязан
держать свой регрессионный порог, собирая стадии либо из уже прогретого
workdir на диске, либо (холодный CI) с нуля из замороженной кассеты
(ревью PR #12, круг 3 — LLM_PROVIDER=gemini обязателен: ключ кассеты
зависит от модели, gemini != anthropic-дефолт). Skip остаётся только если
ни прогретого workdir, ни непустой кассеты нет вовсе — как у тестов мутаций.

Улучшили извлечение/сшивку — поднимите порог тем же коммитом.
"""

import json
from pathlib import Path

import pytest

import solve
from score import score

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")
CASSETTE_DIR = Path("eval/cassette")
EXTRACTED_BASELINE = 30.0


@pytest.fixture(scope="module")
def warmed_workdir(monkeypatch_module=None):
    from ledger import extract_archive
    from util import workdir

    ds_hash, _ = extract_archive(PUBLIC_ZIP)
    wd = workdir(ds_hash)
    warm = (wd / "facts").is_dir() and (wd / "specs").is_dir()
    cassette_ready = CASSETTE_DIR.is_dir() and any(CASSETTE_DIR.glob("*.json"))
    if not warm and not cassette_ready:
        pytest.skip("публичный workdir не прогрет и кассета пуста (нечем собрать стадии офлайн)")
    return wd


def test_extracted_offline_score_not_below_baseline(warmed_workdir, monkeypatch):
    monkeypatch.setenv("LLM_OFFLINE", "1")  # ноль сетевых вызовов: только артефакты и кэш
    monkeypatch.setenv("LLM_PROVIDER", "gemini")  # кассета заморожена под gemini, не под дефолт
    answers = solve.main(PUBLIC_ZIP, facts_source="extracted")
    gt = json.loads(Path("dataset/agentic-bank-public/ground_truth.json").read_text())["scenarios"]
    total = score(answers, gt)
    assert total >= EXTRACTED_BASELINE, f"extracted-прогон просел: {total:.2f} < {EXTRACTED_BASELINE}"
