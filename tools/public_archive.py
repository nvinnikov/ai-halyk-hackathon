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


# Версия формата упаковки: 2 — ZipInfo с фиксированными эпохой и правами
# вместо zf.write по mtime. Лежит рядом с архивом, потому что архив в git не
# хранится: без маркера на машине, где zip собран прежним упаковщиком, ранний
# выход по ARCHIVE.exists() оставил бы его как есть, и локальный dataset_hash
# остался бы плавающим и расходящимся с CI — ровно та воспроизводимость,
# ради которой формат и менялся. Инкремент версии пересобирает архив сам.
PACK_FORMAT = 2
FORMAT_MARKER = ARCHIVE.with_suffix(".packfmt")


def build_public_archive(force: bool = False) -> Path:
    """Собрать публичный архив, если его нет или он собран прежним форматом."""
    fresh = (
        ARCHIVE.exists() and FORMAT_MARKER.exists() and FORMAT_MARKER.read_text().strip() == str(PACK_FORMAT)
    )
    if fresh and not force:
        return ARCHIVE
    out = pack_dataset(DATASET, ARCHIVE)
    FORMAT_MARKER.write_text(f"{PACK_FORMAT}\n")
    return out


if __name__ == "__main__":
    print(build_public_archive())
