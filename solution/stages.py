"""Идемпотентность стадий: артефакт готов, когда совпала версия стадии.

Механика та же, что у кэша LLM: инвалидация по содержимому (версия стадии
входит в артефакт), никогда по времени. Отпечаток входа обеспечивается тем,
что все пути лежат под work/<dataset_hash>/.

Важно: правка кода стадии без инкремента её версии молча переиспользует
старый артефакт — инкремент версии обязателен при любой правке build-логики.
"""

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path

from util import stable_json


def artifact(path: Path, version: int, build: Callable[[], dict]) -> dict:
    """Получить артефакт стадии с версионированием.

    Если файл существует и его версия совпадает с переданной — вернуть готовый
    артефакт. Иначе — запустить build(), записать результат атомарно с версией.

    Args:
        path: Путь к файлу артефакта
        version: Версия стадии, должна совпадать с _meta.stage_version
        build: Функция, возвращающая dict с данными артефакта

    Returns:
        Словарь с результатом build() и добавленной _meta.stage_version
    """
    if path.exists():
        data = json.loads(path.read_text())
        if data.get("_meta", {}).get("stage_version") == version:
            return data
    data = build()
    data["_meta"] = {"stage_version": version}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(stable_json(data))
    tmp.replace(path)
    return data
