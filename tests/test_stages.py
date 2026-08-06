"""Артефакт готов, только когда совпала версия произведшей его стадии."""

import json

from stages import artifact


def test_builds_once_then_reuses(tmp_path):
    p = tmp_path / "a.json"
    calls = []

    def build():
        calls.append(1)
        return {"x": 1}

    assert artifact(p, 1, build)["x"] == 1
    assert artifact(p, 1, build)["x"] == 1
    assert len(calls) == 1  # второй вызов взял готовое


def test_version_bump_rebuilds(tmp_path):
    p = tmp_path / "a.json"
    artifact(p, 1, lambda: {"x": 1})
    got = artifact(p, 2, lambda: {"x": 2})
    assert got["x"] == 2
    assert json.loads(p.read_text())["_meta"]["stage_version"] == 2


def test_write_is_atomic(tmp_path, monkeypatch):
    # незавершённая запись не должна оставить битый артефакт
    p = tmp_path / "a.json"
    artifact(p, 1, lambda: {"x": 1})

    # сохраним содержимое первой версии
    original_content = p.read_text()

    # симулируем ошибку при перемещении tmp в финальный путь
    def failing_replace(self, target):
        raise RuntimeError("Atomic write failed")

    monkeypatch.setattr(type(p), "replace", failing_replace)

    # попытка перестроить на версию 2 должна упасть
    try:
        artifact(p, 2, lambda: {"x": 2})
    except RuntimeError:
        pass

    # стиль должен остаться нетронутым
    assert p.read_text() == original_content
    assert json.loads(p.read_text())["_meta"]["stage_version"] == 1
