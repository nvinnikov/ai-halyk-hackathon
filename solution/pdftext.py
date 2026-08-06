"""Постраничное извлечение текста и детектор слепоты (5.1).

Правило: страница слепая, если после нормализации меньше 200 символов
И меньше 3 числовых токенов. Такие страницы уходят в vision.
"""

import hashlib
import re
from pathlib import Path

from pypdf import PdfReader

from stages import artifact

TEXT_VERSION = 1
_MIN_CHARS = 200
_MIN_NUMBERS = 3
_NUM = re.compile(r"\d[\d,.]*")


def doc_hash(pdf_path: Path) -> str:
    return hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:12]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_blind(text: str) -> bool:
    """«И», не «ИЛИ»: «или» на замере дало 115 слепых страниц, из них 106
    ложных — титулы и оглавления с нормальным текстом, но <3 числами; «и»
    даёт 9, включая оба известных vision-кейса. Ложный vision-вызов не просто
    дорог — он подменяет уже извлечённый текст ответом модели в
    route.full_text, то есть рискует точностью."""
    t = _normalize(text)
    return len(t) < _MIN_CHARS and len(_NUM.findall(t)) < _MIN_NUMBERS


def extract_pages(wd: Path, pdf_path: Path) -> dict:
    def build() -> dict:
        pages = []
        for i, page in enumerate(PdfReader(pdf_path).pages, start=1):
            try:
                text = _normalize(page.extract_text() or "")
            except Exception:
                text = ""
            pages.append({"n": i, "text": text, "blind": is_blind(text)})
        return {"file": pdf_path.name, "pages": pages}

    return artifact(wd / "text" / f"{doc_hash(pdf_path)}.json", TEXT_VERSION, build)
