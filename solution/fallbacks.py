"""Лестница фолбэков (5.7): спека → шаблон по сигнатуре → эвристика по типу →
приор + порог/медиана. null в actual не существует как состояние.

Приор считан скриптом из публичного ключа (eval/prior.py) и условен по
(направление, семья метрики); безопасного дефолта нет — 17/19.

Прочтение 5.7 про «шаблон по сигнатуре»: если метрика спеки не распарсилась,
сигнатуры не существует — поэтому ярусом после DSL идёт эвристика по цитате
(heuristic_template), а сигнатурный матч живёт в specs_extract для валидных спек.
"""

import json

from dsl import Ratio
from util import ROOT

# Через ROOT, а не cwd: запуск не из корня уронил бы skeleton() до первой
# ячейки — submission не создался бы вообще, отменяя весь скелет-первым.
_PRIOR_PATH = ROOT / "eval" / "prior.json"
_prior_cache: dict | None = None

# ключевые слова цитаты пункта → имя шаблона (ярус «эвристика по типу ковенанта»)
_KEYWORDS = [
    ("related_abs", ("related", "связанн", "аффилир")),
    ("capex", ("capital expenditure", "capex", "капитальн")),
    ("revenue", ("revenue", "выручк")),
    ("icr", ("interest cover", "процентн", "icr")),
    ("insurance_cover", ("insurance", "страхов")),
]


def load_prior() -> dict:
    global _prior_cache
    if _prior_cache is None:
        _prior_cache = json.loads(_PRIOR_PATH.read_text())
    return _prior_cache


def family_of(metric_ast, limit) -> str | None:
    if metric_ast is None:
        return None
    if isinstance(metric_ast, Ratio):
        if limit is not None and limit <= 1:
            return "share"
        return "ratio"
    return "absolute"


def _argmax(counts: dict) -> str:
    return max(sorted(counts), key=lambda k: counts[k])


def prior_status(prior: dict, direction: str | None, family: str | None) -> tuple[str, bool]:
    key = f"{direction}|{family}"
    if direction and family and key in prior["by"]:
        return _argmax(prior["by"][key]), True
    return _argmax(prior["global"]), False


def heuristic_template(clause_text: str) -> str | None:
    t = clause_text.lower()
    for name, needles in _KEYWORDS:
        if any(n in t for n in needles):
            return name
    return None


def _median(values: list[float]) -> float:
    vs = sorted(values)
    n = len(vs)
    mid = n // 2
    return vs[mid] if n % 2 else (vs[mid - 1] + vs[mid]) / 2


def fallback_cell(direction, family, limit, computed) -> tuple[dict, list[str]]:
    prior = load_prior()
    status, conditional = prior_status(prior, direction, family)
    alarms = ["fallback_used"] + ([] if conditional else ["fallback_coin_flip"])
    if limit is not None:
        actual = float(limit)
    else:
        same_dir = [a for d, a in computed if d == direction]
        actual = _median(same_dir) if same_dir else 1.0
    return {"status": status, "actual": actual, "evidence_txn_id": None}, alarms
