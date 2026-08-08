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


def test_cache_if_false_returns_but_does_not_write(tmp_path):
    # Деградированный fail-open результат возвращается, но не переживает
    # перезапуск (ревью PR #9, 22-я волна): следующий вызов пересобирает.
    p = tmp_path / "stage.json"
    bad = lambda: {"alarms": [{"kind": "extraction_failed"}]}  # noqa: E731
    ok = lambda: {"alarms": []}  # noqa: E731
    not_degraded = lambda d: not d["alarms"]  # noqa: E731

    art = artifact(p, 1, bad, cache_if=not_degraded)
    assert art["alarms"] and not p.exists()

    art2 = artifact(p, 1, ok, cache_if=not_degraded)
    assert art2["alarms"] == [] and p.exists()

    def boom():
        raise AssertionError("build не должен вызываться при попадании в кэш")

    assert artifact(p, 1, boom, cache_if=not_degraded)["alarms"] == []


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
