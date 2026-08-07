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


# Эпоха zip (1980-01-01): фиксированная метка вместо mtime файла. zf.write()
# берёт date_time из ZipInfo.from_file, поэтому те же байты датасета давали
# разные архивы — а dataset_hash считается от байтов zip. Плыло дважды:
# mutate_ledger переписывает CSV, и его mtime всегда «сейчас»; git clone
# ставит mtime всем файлам в момент клонирования, так что свежий чекаут и CI
# получали свой хеш для того же датасета. Права тоже фиксируются: umask
# машины иначе попадал бы в архив.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_ZIP_MODE = 0o644 << 16


def pack_dataset(dataset_dir: Path, dst_zip: Path) -> Path:
    """Упаковать каталог датасета в zip воспроизводимо: порядок записей
    отсортирован, время и права фиксированы. Одинаковое содержимое даёт
    одинаковые байты, а значит один и тот же dataset_hash и один work/.

    Верхний уровень внутри архива — имя каталога, как в оригинальной раздаче.
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
                info = zipfile.ZipInfo(str(path.relative_to(dataset_dir.parent)), date_time=_ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = _ZIP_MODE
                zf.writestr(info, path.read_bytes())
    tmp.replace(dst_zip)
    return dst_zip


def build_public_archive(force: bool = False) -> Path:
    """Собрать публичный архив, если его нет."""
    if ARCHIVE.exists() and not force:
        return ARCHIVE
    return pack_dataset(DATASET, ARCHIVE)


if __name__ == "__main__":
    print(build_public_archive())
