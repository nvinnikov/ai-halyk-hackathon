"""Слепота — свойство страницы: у 2ed0b2ee4b57.pdf 4374 символа на файл,
но страницы 3–4 не отдают текстовый слой."""

from pathlib import Path

from pdftext import doc_hash, extract_pages, is_blind

DOCS = Path("dataset/agentic-bank-public/documents")


def test_is_blind_short_text():
    assert is_blind("почти пусто")
    assert is_blind("")


def test_text_without_numbers_is_not_blind():
    # Титулы и оглавления: нормальный текст, но <3 числовых токенов. Правило
    # «ИЛИ» пометило бы их слепыми (106 ложных из 115 на замере) — правило «И» нет.
    assert not is_blind("длинный связный текст без чисел " * 20)


def test_not_blind_normal_page():
    assert not is_blind("Договор займа на сумму 1,000,000.00 от 2025-01-01, ставка 12.5% " * 10)


def test_known_partially_blind_document(tmp_path):
    art = extract_pages(tmp_path, DOCS / "2ed0b2ee4b57.pdf")
    blind = [p["n"] for p in art["pages"] if p["blind"]]
    assert 3 in blind and 4 in blind
    assert 1 not in blind


def test_doc_hash_stable():
    p = DOCS / "2ed0b2ee4b57.pdf"
    assert doc_hash(p) == doc_hash(p) and len(doc_hash(p)) == 12


def test_artifact_reused(tmp_path):
    p = DOCS / "2ed0b2ee4b57.pdf"
    a = extract_pages(tmp_path, p)
    b = extract_pages(tmp_path, p)
    assert a["pages"] == b["pages"]
    assert (tmp_path / "text" / f"{doc_hash(p)}.json").exists()
