"""Слепая страница рендерится в одностраничный PDF и читается vision-моделью."""

import base64
import json
from pathlib import Path

import pytest

import vision

DOCS = Path("dataset/agentic-bank-public/documents")
PDF = DOCS / "2ed0b2ee4b57.pdf"


def test_reads_page_via_llm(tmp_path, monkeypatch):
    seen = {}

    def fake_call(prompt, schema, schema_version, document_b64=None, max_tokens=2000):
        seen["doc"] = document_b64
        return {"text": "distilled page"}

    monkeypatch.setattr(vision.llm, "call", fake_call)
    got = vision.read_blind_page(tmp_path, PDF, 3)
    assert got == "distilled page"
    # в модель ушёл валидный одностраничный PDF
    raw = base64.b64decode(seen["doc"])
    assert raw.startswith(b"%PDF")

    # артефакт лежит на диске и переиспользуется без повторного вызова
    monkeypatch.setattr(vision.llm, "call", lambda *a, **k: pytest.fail("не должен вызываться"))
    assert vision.read_blind_page(tmp_path, PDF, 3) == "distilled page"
    art = json.loads((tmp_path / "vision" / f"{vision.doc_hash(PDF)}.p3.json").read_text())
    assert art["text"] == "distilled page"


@pytest.mark.llm
def test_live_vision_recovers_numbers(tmp_path):
    """Живой вызов: страницы 3–4 2ed0b2ee4b57.pdf должны отдать числа таблицы добавок."""
    text = vision.read_blind_page(tmp_path, PDF, 3) + vision.read_blind_page(tmp_path, PDF, 4)
    assert any(ch.isdigit() for ch in text)
