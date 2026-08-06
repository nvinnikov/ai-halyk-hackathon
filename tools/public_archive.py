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


def pack_dataset(dataset_dir: Path, dst_zip: Path) -> Path:
    """Упаковать каталог датасета в zip. Верхний уровень внутри архива —
    имя каталога, как в оригинальной раздаче; порядок записей отсортирован,
    поэтому две сборки подряд дают один и тот же файл.

    Единственная реализация упаковки на всех потребителей: публичный архив,
    мутированный архив (eval/mutations_ledger.py), CI и conftest."""
    dataset_dir = Path(dataset_dir)
    dst_zip = Path(dst_zip)
    assert dataset_dir.is_dir(), f"нет каталога датасета: {dataset_dir}"
    tmp = dst_zip.with_name(dst_zip.name + ".tmp")
    dst_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(dataset_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(dataset_dir.parent))
    tmp.replace(dst_zip)
    return dst_zip


def build_public_archive(force: bool = False) -> Path:
    """Собрать публичный архив, если его нет."""
    if ARCHIVE.exists() and not force:
        return ARCHIVE
    return pack_dataset(DATASET, ARCHIVE)


if __name__ == "__main__":
    print(build_public_archive())
