"""Сборка закрытого архива датасета из dataset/agentic-bank-private/.

Зеркало `tools/public_archive.py`: тот же воспроизводимый упаковщик
(`pack_dataset`), другой каталог-источник и другое имя архива. Вход
пайплайна (`./run.sh <архив>`) — архив, но в git он не хранится (`*.zip` в
`.gitignore`) — набор лежит распакованным каталогом, как и публичный, чтобы
договоры можно было открыть глазами, а не только распаковкой блоба.

Каталог называется `agentic-bank-private/`, а не `agentic-bank-hidden` — по
той же логике, что и `agentic-bank-public/`: пара «публичный/приватный»
читается сама, «public/hidden» заставляет догадываться. Организаторы
раздавали набор под именем `agentic-bank-hidden/` (это и есть его верхний
уровень внутри `6a7819a8cb7d3480322468.zip`, полученного от команды из
топ-10, — см. `docs/ops/private-set-postmortem.md`); в репозитории оно не
сохранено, но упаковщик кладёт архиву своё имя каталога верхним уровнем, так
что после пересборки внутри архива будет `agentic-bank-private/` — конвейер
на конкретное имя каталога не завязан (`ledger.find_inputs` ищет файлы по
содержимому и расположению, не по имени).

Наш упаковщик не байт-идентичен организаторскому, поэтому `dataset_hash`
закрытого набора меняется при пересборке — это ожидаемо (кассета привязана
к тексту промпта, а не к хешу набора) и не ломает офлайн-прогон. Именно
поэтому `tools/verify_private.py` сверяет не архив целиком, а файлы внутри
него, чьи хеши от упаковки не зависят: леджер и шаблон ответа.

Собирают архив: `make private-archive` и `tests/test_reality_gate.py`
(последний — только на машинах с приватной репликой, гейт скипается везде
ещё). Реализация упаковки одна на всех потребителей — см. `pack_dataset`.
"""

from pathlib import Path

from public_archive import PACK_FORMAT, pack_dataset

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "dataset" / "agentic-bank-private"
# Имя от организаторов: закрытый набор раздавался именно этим файлом.
ARCHIVE = ROOT / "6a7819a8cb7d3480322468.zip"

FORMAT_MARKER = ARCHIVE.with_suffix(".packfmt")


def build_private_archive(force: bool = False) -> Path:
    """Собрать закрытый архив, если его нет или он собран прежним форматом."""
    fresh = (
        ARCHIVE.exists() and FORMAT_MARKER.exists() and FORMAT_MARKER.read_text().strip() == str(PACK_FORMAT)
    )
    if fresh and not force:
        return ARCHIVE
    out = pack_dataset(DATASET, ARCHIVE)
    FORMAT_MARKER.write_text(f"{PACK_FORMAT}\n")
    return out


if __name__ == "__main__":
    print(build_private_archive())
