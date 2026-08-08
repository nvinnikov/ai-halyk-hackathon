"""Гейты архива в Makefile: цели run/sanity не работают по умолчанию.

Гейт поставлен, чтобы забытое `ARCHIVE=` в окне не посчитало публичный набор
поверх приватного submission. Критериев два, и они разные, потому что разная
цена ошибки:

- `require-archive` (sanity) — только происхождение переменной. Sanity ничего
  не пишет, а предполётная проверка стоп-строки из ранбука устроена как
  `make sanity ARCHIVE=<публичный>`, и запрет по значению её бы заблокировал;
- `require-private-archive` (run) — происхождение И значение. Run перезаписывает
  отправляемый `out/submission.json`, и публичный архив здесь не бывает верным
  ни при каком раскладе: публичный набор гоняется через `make solve`.

Цели зовутся напрямую: зависимости от install у них нет, поэтому проверка не
тянет `uv sync` и идёт мгновенно.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_ARCHIVE = "6a741640c31eb032062683.zip"

# Переменные, которые ОБЯЗАНЫ быть вычищены из окружения дочернего make:
# критерий гейта — происхождение ARCHIVE, поэтому унаследованное значение
# сделало бы тест зависимым от того, из какой оболочки его запустили.
# ARCHIVE — прямая протечка (`export ARCHIVE=...` в ранбуке, строка 30).
# MAKEFLAGS — косвенная: GNU make дублирует туда переменные командной строки,
# и вложенный make возвращает их обратно уже как `command line` (проверено:
# `make outer ARCHIVE=foo` даёт во вложенном `origin=command line`). Без
# вычистки `make eval-offline ARCHIVE=$ARCHIVE` красил бы этот тест, а красный
# `make check` по ранбуку означает откат на последний зелёный коммит.
_STRIPPED = ("ARCHIVE", "MAKEFLAGS")


def _make(target: str, args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if k not in _STRIPPED}
    return subprocess.run(
        ["make", target, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**clean, **(env or {})},
    )


# --- require-archive: гейт sanity, критерий только по происхождению ----------


def test_guard_blocks_when_archive_not_given():
    r = _make("require-archive", [])
    assert r.returncode != 0, "гейт пропустил прогон без ARCHIVE"
    assert "ARCHIVE не задан" in r.stdout + r.stderr


def test_guard_allows_public_archive_named_explicitly():
    """Предполётная проверка ранбука («стоп-проверка жива») гоняет sanity
    именно по публичному архиву — гейт обязан её пропустить."""
    r = _make("require-archive", [f"ARCHIVE={PUBLIC_ARCHIVE}"])
    assert r.returncode == 0, f"гейт заблокировал явно названный публичный архив: {r.stdout}{r.stderr}"


def test_guard_allows_private_archive_from_command_line():
    r = _make("require-archive", ["ARCHIVE=/tmp/private.zip"])
    assert r.returncode == 0, f"гейт заблокировал явный архив: {r.stdout}{r.stderr}"


def test_guard_allows_archive_from_environment():
    """`export ARCHIVE=...` — форма из ранбука; `?=` не перебивает окружение,
    и значение уже настоящее, а не дефолт."""
    r = _make("require-archive", [], env={"ARCHIVE": "/tmp/private.zip"})
    assert r.returncode == 0, f"гейт заблокировал ARCHIVE из окружения: {r.stdout}{r.stderr}"


def test_guard_ignores_archive_leaked_from_environment():
    """Гейт судит по происхождению, но тест обязан судить по Makefile: с
    ARCHIVE в окружении оболочки проверка «не задали» осталась бы без
    предмета. Здесь окружение вычищено — блокировка обязана сработать."""
    r = _make("require-archive", [], env={})
    assert r.returncode != 0, "унаследованный ARCHIVE протёк в дочерний make"


# --- require-private-archive: гейт run, происхождение И значение -------------


def test_run_guard_blocks_when_archive_not_given():
    r = _make("require-private-archive", [])
    assert r.returncode != 0, "гейт run пропустил прогон без ARCHIVE"
    assert "ARCHIVE не задан" in r.stdout + r.stderr


def test_run_guard_blocks_public_archive_named_explicitly():
    """У run публичный архив не бывает верным: он перезаписывает отправляемый
    submission. Реалистичный путь — `export ARCHIVE=<публичный>` остался в
    оболочке с репетиции."""
    r = _make("require-private-archive", [f"ARCHIVE={PUBLIC_ARCHIVE}"])
    assert r.returncode != 0, "гейт run пропустил публичный архив"
    assert "публичный архив" in r.stdout + r.stderr


def test_run_guard_blocks_public_archive_from_environment():
    r = _make("require-private-archive", [], env={"ARCHIVE": PUBLIC_ARCHIVE})
    assert r.returncode != 0, "гейт run пропустил публичный архив из окружения"


def test_run_guard_allows_private_archive():
    r = _make("require-private-archive", ["ARCHIVE=/tmp/private.zip"])
    assert r.returncode == 0, f"гейт run заблокировал приватный архив: {r.stdout}{r.stderr}"
