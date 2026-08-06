"""Модули solution/* читают датасет по путям от корня репозитория и импортируют
друг друга плоско, поэтому тесты фиксируют и cwd, и sys.path."""

import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "solution"))
sys.path.insert(0, str(ROOT / "eval"))

# Собрать архив датасета для CI, если его нет.
# В CI файл в .gitignore отсутствует, поэтому пересоздаём из закоммиченного датасета.
_ZIP_PATH = ROOT / "6a741640c31eb032062683.zip"
_DATASET_PATH = ROOT / "dataset" / "agentic-bank-public"

if not _ZIP_PATH.exists() and _DATASET_PATH.exists():
    # Архивируем dataset/agentic-bank-public/ так, чтобы верхнеуровневый каталог
    # в архиве был agentic-bank-public/ (архивируем из dataset/)
    with zipfile.ZipFile(_ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in _DATASET_PATH.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(_DATASET_PATH.parent)
                zf.write(file_path, arcname)
