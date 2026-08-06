"""Сборка публичного архива датасета из dataset/agentic-bank-public/.

Вход пайплайна — архив (`./run.sh <архив>`), но в git он не хранится
(`*.zip` в .gitignore). Собирают его трое: `make public-archive`, шаг CI и
`tests/conftest.py`. Реализация обязана быть одна: разные способы упаковки
дают разные байты, а значит разный dataset_hash и разные каталоги work/.

На боевом прогоне архив приходит аргументом и уже существует — это только
про разработку и репетицию.
"""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "dataset" / "agentic-bank-public"
# Имя от организаторов: публичный набор раздавался именно этим файлом.
ARCHIVE = ROOT / "6a741640c31eb032062683.zip"


def build_public_archive(force: bool = False) -> Path:
    """Собрать архив, если его нет. Верхний уровень внутри архива —
    `agentic-bank-public/`, как в оригинальной раздаче. Порядок записей
    отсортирован: две сборки подряд дают один и тот же файл."""
    if ARCHIVE.exists() and not force:
        return ARCHIVE
    assert DATASET.is_dir(), f"нет каталога датасета: {DATASET}"
    tmp = ARCHIVE.with_name(ARCHIVE.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DATASET.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(DATASET.parent))
    tmp.replace(ARCHIVE)
    return ARCHIVE


if __name__ == "__main__":
    print(build_public_archive())
