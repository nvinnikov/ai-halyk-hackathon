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

from public_archive import DATASET, build_public_archive

# Архив датасета в git не хранится (*.zip в .gitignore) — на свежем клоне и в CI
# собираем из закоммиченного датасета. Той же функцией, что `make public-archive`:
# другая упаковка дала бы другие байты и другой dataset_hash.
if DATASET.is_dir():
    build_public_archive()
