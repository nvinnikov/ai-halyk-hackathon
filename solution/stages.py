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


def artifact(
    path: Path,
    version: int,
    build: Callable[[], dict],
    cache_if: Callable[[dict], bool] | None = None,
) -> dict:
    """Получить артефакт стадии с версионированием.

    Если файл существует и его версия совпадает с переданной — вернуть готовый
    артефакт. Иначе — запустить build(), записать результат атомарно с версией.

    Args:
        path: Путь к файлу артефакта
        version: Версия стадии, должна совпадать с _meta.stage_version
        build: Функция, возвращающая dict с данными артефакта
        cache_if: Предикат по результату build(); False — результат вернуть,
            но НА ДИСК НЕ ПИСАТЬ. Инвалидация только по версии означает, что
            деградированный fail-open результат (провал извлечения, запечённый
            как валидный dict) пережил бы перезапуск после устранения причины
            (ревью PR #9, 20-я и 22-я волны, docs/ops/recovery-playbook.md).

    Returns:
        Словарь с результатом build() и добавленной _meta.stage_version
    """
    if path.exists():
        data = json.loads(path.read_text())
        if data.get("_meta", {}).get("stage_version") == version:
            return data
    data = build()
    data["_meta"] = {"stage_version": version}
    if cache_if is not None and not cache_if(data):
        return data
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(stable_json(data))
    tmp.replace(path)
    return data
