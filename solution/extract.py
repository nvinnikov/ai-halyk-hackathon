"""Извлекает текст из всех PDF датасета в один JSON-кэш."""

import glob
import json
import os
import re
import sys

from pypdf import PdfReader

DOCS = sys.argv[1] if len(sys.argv) > 1 else "dataset/agentic-bank-public/documents"
OUT = sys.argv[2] if len(sys.argv) > 2 else "solution/docs_text.json"

cache = {}
for path in sorted(glob.glob(os.path.join(DOCS, "*"))):
    name = os.path.basename(path)
    if not name.lower().endswith(".pdf"):
        continue
    try:
        text = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    except Exception as exc:
        text = ""
        print(f"! {name}: {exc}")
    cache[name] = re.sub(r"[ \t]+", " ", text)

json.dump(cache, open(OUT, "w"), ensure_ascii=False)
empty = [k for k, v in cache.items() if len(v.strip()) < 60]
print(f"{len(cache)} pdf, пустых (нужен OCR): {empty}")
