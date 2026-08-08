"""Гейты архива в Makefile: цели run/sanity не работают по умолчанию.

Гейт поставлен, чтобы забытое `ARCHIVE=` в окне не посчитало публичный набор
поверх приватного submission. Критериев два, и они разные, потому что разная
цена ошибки:

- `require-archive` (sanity) — только происхождение переменной. Sanity ничего
  не пишет, а предполётная проверка стоп-строки из ранбука устроена как
  `make sanity ARCHIVE=<публичный>`, и запрет по значению её бы заблокировал;
- `require-private-archive` (run) — происхождение И содержимое. Run перезаписывает
  отправляемый `out/submission.json`, и публичный набор здесь не бывает верным
  ни при каком раскладе: он гоняется через `make solve`. Сравнение побайтовое,
  а не по имени файла: имя публичного архива — это имя от организаторов, и
  ничто не обещает, что приватный приедет под другим.

Цели зовутся напрямую: зависимости от install у них нет, поэтому проверка не
тянет `uv sync` и идёт мгновенно.
"""

import os
import shutil
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


def test_guard_ignores_archive_leaked_from_environment(monkeypatch):
    """Гейт судит по происхождению, но тест обязан судить по Makefile: с
    ARCHIVE в окружении оболочки проверка «не задали» осталась бы без предмета.

    Протечка моделируется ЯВНО (ревью PR #18, круг 3): без setenv тест
    побайтово повторял бы соседний и остался бы зелёным на чистой машине даже
    с пустым _STRIPPED — покраснел бы только у того, у кого `export ARCHIVE=`
    в оболочке, то есть ровно тем ложным красным `make check` в окне, от
    которого и защищались. MAKEFLAGS — второй канал: GNU make возвращает через
    него переменные командной строки вложенному make уже как `command line`.
    """
    monkeypatch.setenv("ARCHIVE", "/tmp/leaked.zip")
    monkeypatch.setenv("MAKEFLAGS", "ARCHIVE=/tmp/leaked.zip")
    r = _make("require-archive", [])
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
    assert "совпал с публичным" in r.stdout + r.stderr


def test_run_guard_blocks_public_archive_from_environment():
    r = _make("require-private-archive", [], env={"ARCHIVE": PUBLIC_ARCHIVE})
    assert r.returncode != 0, "гейт run пропустил публичный архив из окружения"


def test_run_guard_blocks_public_archive_written_as_path(tmp_path):
    """Форма из ранбука — `export ARCHIVE=/путь/к/…zip`, а не голое имя, так
    что протёкший с репетиции export с большей вероятностью будет путём
    (ревью PR #18, круг 2). Копия в другом каталоге и под другим именем — это
    по-прежнему публичный набор, и сравнение содержимого это видит."""
    copy = tmp_path / "rehearsal-leftover.zip"
    shutil.copyfile(ROOT / PUBLIC_ARCHIVE, copy)
    for spelling in (f"./{PUBLIC_ARCHIVE}", str(copy)):
        r = _make("require-private-archive", [f"ARCHIVE={spelling}"])
        assert r.returncode != 0, f"гейт run пропустил публичный архив как {spelling}"
        assert "совпал с публичным" in r.stdout + r.stderr


def test_run_guard_allows_private_archive_sharing_the_public_name(tmp_path):
    """Ложный красный в окне дороже пропуска (ревью PR #18, круг 6).

    `6a741640c31eb032062683.zip` — имя, которым организаторы раздали публичный
    набор, и ничто не обещает, что приватный приедет под другим. Гейт по имени
    отказал бы 9 августа на НАСТОЯЩЕМ архиве, да ещё и советом считать
    публичный набор. Судить надо по содержимому.
    """
    private = tmp_path / PUBLIC_ARCHIVE
    private.write_bytes("PK\x03\x04 это другой набор".encode())
    r = _make("require-private-archive", [f"ARCHIVE={private}"])
    assert r.returncode == 0, f"гейт отказал приватному архиву из-за имени: {r.stdout}{r.stderr}"


def test_run_guard_allows_private_archive():
    r = _make("require-private-archive", ["ARCHIVE=/tmp/private.zip"])
    assert r.returncode == 0, f"гейт run заблокировал приватный архив: {r.stdout}{r.stderr}"


# --- проводка гейтов к целям, которые пишут out/submission.json --------------


_PRIVATE_GUARD_MARK = "совпал с публичным"  # текст только из require-private-archive
_ANY_GUARD_MARK = "ARCHIVE не задан"  # текст из require-archive, печатается и в обоих случаях


def test_run_target_is_gated_by_private_archive_guard():
    """`run` — самая дорогая цель: она перезаписывает отправляемый файл, и
    именно ради неё вводится require-private-archive (ревью PR #18, круг 9).
    Тесты выше зовут гейты напрямую и проводку не проверяют: снос зависимости
    в строке `run:` или подмена её на более слабый require-archive оставили бы
    их все зелёными.

    Совпадение ищется по тексту ИМЕННО сильного гейта: require-private-archive
    зависит от require-archive, поэтому текст слабого печатается в обоих
    случаях и подмену не различил бы.
    """
    r = _make("run", ["--dry-run"])
    assert _PRIVATE_GUARD_MARK in r.stdout + r.stderr, "цель run не проходит через require-private-archive"


def test_sanity_target_is_gated_by_weak_guard_only():
    """У `sanity` гейт обязан остаться слабым: на ней держится предполётная
    проверка стоп-строки, которая гоняется ПО ПУБЛИЧНОМУ архиву. Сильный гейт
    здесь сломал бы её — это ровно та ошибка, с которой начался круг 1, только
    в другую сторону."""
    r = _make("sanity", ["--dry-run"])
    out = r.stdout + r.stderr
    assert _ANY_GUARD_MARK in out, "цель sanity не проходит через require-archive"
    assert _PRIVATE_GUARD_MARK not in out, "sanity гейтится сильным гейтом — предполётная проверка сломана"


def test_determinism_target_is_gated():
    """`determinism` зовёт ./run.sh дважды и перезаписывает отправляемый файл,
    так что забытое ARCHIVE= стоит ей того же, что и run (ревью PR #18,
    круг 2). Гейт мягче — это репетиционная цель, публичный архив ей нужен.

    Проверка через `make -n`: рецепты печатаются, но не исполняются, поэтому
    отсутствие гейта не запустит настоящий прогон прямо в тестах. Прямой вызов
    цели здесь недопустим ровно поэтому.
    """
    r = _make("determinism", ["--dry-run"])
    out = r.stdout + r.stderr
    assert "ARCHIVE не задан" in out, "цель determinism не проходит через require-archive"
