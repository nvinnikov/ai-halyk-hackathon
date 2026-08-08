"""Модули solution/* читают датасет по путям от корня репозитория и импортируют
друг друга плоско, поэтому тесты фиксируют и cwd, и sys.path."""

import os
import sys
from pathlib import Path

import pytest

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


def _out_snapshot(out_dir: Path) -> dict[str, bytes]:
    if not out_dir.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in sorted(out_dir.glob("*.json"))}


@pytest.fixture(scope="module", autouse=True)
def isolated_out(tmp_path_factory):
    """Каталог вывода solve на время модуля — временный, а не боевой out/.

    solve.py биндит OUT именем при импорте (`from util import OUT`), поэтому
    любой вызов solve.main из тестов писал бы submission.json и run-report.json
    поверх РЕАЛЬНОГО out/ — того самого файла, который уходит организаторам
    (тот же приём изоляции, что в eval/mutations.py). Фикстура autouse: новый
    тест, зовущий solve.main, защищён по умолчанию, а не после того, как о нём
    вспомнят. Область модульная — внутри модуля порядок записи в общий каталог
    сохраняется ровно такой, каким был у боевого out/, семантика проверок не
    меняется. Тест, которому нужен сам каталог, запрашивает фикстуру по имени.
    На выходе проверяется, что реальный out/ не изменился ни байтом.
    """
    import solve

    real_out = ROOT / "out"
    before = _out_snapshot(real_out)
    prev = solve.OUT
    solve.OUT = tmp_path_factory.mktemp("out")
    try:
        yield solve.OUT
    finally:
        solve.OUT = prev
        assert before == _out_snapshot(real_out), "тест задел РЕАЛЬНЫЙ out/ — изоляция solve.OUT не сработала"
