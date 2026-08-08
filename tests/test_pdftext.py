"""Слепота — свойство страницы: у 2ed0b2ee4b57.pdf 4374 символа на файл,
но страницы 3–4 не отдают текстовый слой."""

from pathlib import Path

from pdftext import doc_hash, extract_pages, is_blind, is_borderline

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


def test_borderline_is_exactly_one_criterion():
    # Пограничная страница проходит ровно по одному критерию из двух —
    # видима правилом «И», но ослепла бы при «ИЛИ». Рост счётчика на новом
    # наборе — сигнал переключать правило (research §3, оговорка).
    long_no_numbers = "длинный связный текст без единого числа " * 20
    short_many_numbers = "12 34 56 78"
    assert is_borderline(long_no_numbers) and not is_blind(long_no_numbers)
    assert is_borderline(short_many_numbers) and not is_blind(short_many_numbers)


def test_borderline_false_for_blind_and_normal_pages():
    assert not is_borderline("")  # оба критерия — слепая, не пограничная
    assert not is_borderline("Договор займа на сумму 1,000,000.00 от 2025-01-01, ставка 12.5% " * 10)


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


def test_page_footer_number_is_stripped(tmp_path):
    # Футер PDF (голый номер страницы) иначе остаётся приклеенным к концу
    # текста страницы; join страниц в route.full_text протаскивает его в
    # середину предложения на странице, где договорное условие делится
    # ровно по границе страницы — и цитата модели перестаёт совпадать с
    # текстом (verify_quote падает на «лишней» цифре).
    art = extract_pages(tmp_path, DOCS / "61dfc54675dc.pdf")
    for p in art["pages"]:
        assert not p["text"].endswith(f" {p['n']}"), (p["n"], p["text"][-20:])
