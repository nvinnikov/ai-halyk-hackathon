"""Гейт `require-archive` в Makefile: цели run/sanity требуют явный архив.

Гейт поставлен, чтобы забытое `ARCHIVE=` в окне не посчитало публичный набор
поверх приватного submission. Но критерий у него не бесплатный: сравнение
значения с дефолтом не отличает «забыли» от «назвали публичный архив
намеренно», а именно так предполётная проверка стоп-строки из ранбука и
устроена (`make sanity ARCHIVE=<публичный>`). Тест фиксирует обе стороны:
гейт срабатывает, когда переменную не задали, и пропускает, когда задали —
хоть аргументом, хоть окружением.

Цель `require-archive` зовётся напрямую: у неё нет зависимости от install,
поэтому проверка не тянет `uv sync` и идёт мгновенно.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_ARCHIVE = "6a741640c31eb032062683.zip"


def _make(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", "require-archive", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def test_guard_blocks_when_archive_not_given():
    r = _make([])
    assert r.returncode != 0, "гейт пропустил прогон без ARCHIVE"
    assert "ARCHIVE не задан" in r.stdout + r.stderr


def test_guard_allows_public_archive_named_explicitly():
    """Предполётная проверка ранбука («стоп-проверка жива») гоняет sanity
    именно по публичному архиву — гейт обязан её пропустить."""
    r = _make([f"ARCHIVE={PUBLIC_ARCHIVE}"])
    assert r.returncode == 0, f"гейт заблокировал явно названный публичный архив: {r.stdout}{r.stderr}"


def test_guard_allows_private_archive_from_command_line():
    r = _make(["ARCHIVE=/tmp/private.zip"])
    assert r.returncode == 0, f"гейт заблокировал явный архив: {r.stdout}{r.stderr}"


def test_guard_allows_archive_from_environment():
    """`export ARCHIVE=...` — форма из ранбука; `?=` не перебивает окружение,
    и значение уже настоящее, а не дефолт."""
    r = _make([], env={"ARCHIVE": "/tmp/private.zip"})
    assert r.returncode == 0, f"гейт заблокировал ARCHIVE из окружения: {r.stdout}{r.stderr}"
