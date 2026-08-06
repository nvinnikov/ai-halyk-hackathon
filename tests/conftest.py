"""Общие фикстуры для всех тестов."""

import re
import sys
from pathlib import Path

import pytest

# Добавляем solution/ в путь для импорта
sys.path.insert(0, str(Path(__file__).parent.parent / "solution"))

import llm


@pytest.fixture(autouse=True)
def mock_llm_call_for_tests(monkeypatch):
    """Мокирование llm.call для тестов, чтобы не требовать реального LLM-ключа."""

    def fake_call(prompt, schema, schema_version, document_b64=None, max_tokens=8000):
        # На публичном датасете есть 11 уникальных описаний в OTHER:
        # "Sewer discharge levy" с разными периодами.
        # По брифу, все они должны быть классифицированы как UTILITIES.
        descriptions = []
        for line in prompt.split("\n"):
            if re.match(r"^\d+\.\s+", line):
                desc = line.split(". ", 1)[1] if ". " in line else line
                descriptions.append(desc)

        return {"categories": [{"description": desc, "category": "UTILITIES"} for desc in descriptions]}

    monkeypatch.setattr(llm, "call", fake_call)
