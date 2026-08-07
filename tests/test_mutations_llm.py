"""Сквозная проверка мутаций через solve.main — живой прогон, маркер llm.

Мутированный текст — новый ключ кэша llm.call (сколько живых вызовов и на
какие документы — см. task-28-report.md). Юниты самих mutations.py (build_
renamed/shift_threshold/build_fx, guard от холостой мутации) — офлайн, в
test_mutations.py."""

from pathlib import Path

import pytest
from mutations import main

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")


@pytest.mark.llm
def test_rename_does_not_change_answers():
    assert main(PUBLIC_ZIP, "rename")


@pytest.mark.llm
def test_shift_changes_exactly_predicted_status():
    assert main(PUBLIC_ZIP, "shift")


@pytest.mark.llm
def test_fx_round_trips_to_same_answers():
    assert main(PUBLIC_ZIP, "fx")
