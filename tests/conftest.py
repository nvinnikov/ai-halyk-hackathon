"""Модули solution/* читают датасет по путям от корня репозитория и импортируют
друг друга плоско, поэтому тесты фиксируют и cwd, и sys.path."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "solution"))
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "tools"))

# Собрать архив датасета для CI, если его нет (в git он не хранится).
# Сборка одна на всех потребителей: другой способ упаковки дал бы другие
# байты, другой dataset_hash и другой каталог work/.
from public_archive import build_public_archive

if (ROOT / "dataset" / "agentic-bank-public").is_dir():
    build_public_archive()
