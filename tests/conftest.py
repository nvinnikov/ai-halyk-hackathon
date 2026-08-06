"""Модули solution/* читают датасет по путям от корня репозитория и импортируют
друг друга плоско, поэтому тесты фиксируют и cwd, и sys.path."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "solution"))
