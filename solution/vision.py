"""Vision-ветка (5.1): слепые страницы читаются моделью по одной.

Страница вырезается в одностраничный PDF — скан внутри него сохраняется,
а кэш LLM адресуется содержимым, так что повторные прогоны бесплатны и
одинаковая страница в двух наборах законно переиспользует ответ.
"""

import base64
import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter

import llm
from pdftext import doc_hash
from stages import artifact

VISION_VERSION = 1
SCHEMA_VERSION = "vision-1"

VISION_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

VISION_PROMPT = (
    "Это отсканированная страница финансового документа. Перепиши её содержимое "
    "полностью и дословно в markdown. Таблицы передавай markdown-таблицами, все "
    "числа, коды счетов и названия компаний — точно как в оригинале, ничего не "
    "пропускай и не додумывай. Верни результат через emit."
)


def _single_page_pdf_b64(pdf_path: Path, page_n: int) -> str:
    writer = PdfWriter()
    writer.add_page(PdfReader(pdf_path).pages[page_n - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return base64.b64encode(buf.getvalue()).decode()


def read_blind_page(wd: Path, pdf_path: Path, page_n: int) -> str:
    def build() -> dict:
        # max_tokens=8000: adaptive thinking считается внутрь лимита, и полная
        # таблица со скана в 4000 может не поместиться; обрезку по max_tokens
        # клиент ловит сам по stop_reason.
        result = llm.call(
            VISION_PROMPT,
            VISION_SCHEMA,
            SCHEMA_VERSION,
            document_b64=_single_page_pdf_b64(pdf_path, page_n),
            max_tokens=8000,
        )
        return {"text": result["text"]}

    art = artifact(wd / "vision" / f"{doc_hash(pdf_path)}.p{page_n}.json", VISION_VERSION, build)
    return art["text"]
