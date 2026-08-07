"""Постраничное извлечение текста и детектор слепоты (5.1).

Правило: страница слепая, если после нормализации меньше 200 символов
И меньше 3 числовых токенов. Такие страницы уходят в vision.
"""

import hashlib
import re
from pathlib import Path

from pypdf import PdfReader

from stages import artifact

TEXT_VERSION = 2
_MIN_CHARS = 200
_MIN_NUMBERS = 3
_NUM = re.compile(r"\d[\d,.]*")


def doc_hash(pdf_path: Path) -> str:
    return hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:12]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_page_footer(raw_text: str, n: int) -> str:
    """PDF-футер (голый номер страницы) в сыром выводе pypdf лежит на
    отдельной строке в самом конце страницы (проверено на реальных
    документах датасета: "...превышали \\n6"). Join страниц через "\\n"
    (route.full_text) протаскивает эту цифру в середину предложения, если
    договорное условие делится ровно по границе страницы — и дословная
    цитата модели перестаёт совпадать с текстом (verify_quote падает).

    Снимаем строку, только если она ТОЧНО равна номеру этой страницы —
    футер не спутать с суммой или БИН, которые случайно кончаются на то же
    число: сумма живёт в той же строке, что и остальной текст, футер — нет.
    Работаем на сыром тексте (до схлопывания пробелов _normalize),
    иначе граница строки теряется."""
    lines = raw_text.split("\n")
    if lines and lines[-1].strip() == str(n):
        lines.pop()
    return "\n".join(lines)


def blindness_criteria(text: str) -> tuple[bool, bool]:
    """(мало символов, мало чисел) — два критерия слепоты по отдельности.

    Слепота — их конъюнкция, см. is_blind. Порознь они нужны sanity-скрипту:
    страница, проходящая ровно по одному критерию, — пограничная. Порог
    откалиброван на одном наборе, и резкий рост числа пограничных страниц
    означает, что сканов в приватном наборе больше и правило пора переключать
    на «ИЛИ» прямо в окне 9 августа."""
    t = _normalize(text)
    return len(t) < _MIN_CHARS, len(_NUM.findall(t)) < _MIN_NUMBERS


def is_blind(text: str) -> bool:
    """«И», не «ИЛИ»: «или» на замере дало 115 слепых страниц, из них 106
    ложных — титулы и оглавления с нормальным текстом, но <3 числами; «и»
    даёт 9, включая оба известных vision-кейса. Ложный vision-вызов не просто
    дорог — он подменяет уже извлечённый текст ответом модели в
    route.full_text, то есть рискует точностью."""
    few_chars, few_numbers = blindness_criteria(text)
    return few_chars and few_numbers


def extract_pages(wd: Path, pdf_path: Path) -> dict:
    def build() -> dict:
        pages = []
        for i, page in enumerate(PdfReader(pdf_path).pages, start=1):
            try:
                raw = page.extract_text() or ""
                text = _normalize(_strip_page_footer(raw, i))
            except Exception:
                text = ""
            pages.append({"n": i, "text": text, "blind": is_blind(text)})
        return {"file": pdf_path.name, "pages": pages}

    return artifact(wd / "text" / f"{doc_hash(pdf_path)}.json", TEXT_VERSION, build)
