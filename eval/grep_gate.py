"""Греп-гейт на утечки знания за пределы слоя извлечения.

Не пускает захардкоженные имена заёмщиков, номера пунктов, пороги, префиксы
TXN-/ACC- и ID сценариев в solution/ и run.sh. Список запрещённого строится из
eval-данных (FACTS/SPECS), шаблона и официальных форматов.
"""

import json
import re
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

# Источники данных
from expected_extraction import FACTS, SPECS


def _load_scenarios_and_covenants() -> tuple[set[str], set[str]]:
    """ID сценариев и номера пунктов — из публичного шаблона submission.

    Так параметры гейта не хардкодятся и подстраиваются под правки шаблона.
    """
    template_path = Path("dataset/agentic-bank-public/submission_template.json")
    if not template_path.exists():
        # Фолбэк для тестов/офлайн-режима
        return {"P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10", "B1", "B4"}, {
            "6.1",
            "6.2",
            "6.3",
        }

    with open(template_path) as f:
        data = json.load(f)

    scenarios = set(data["answers"].keys())
    covenants = set()
    for scenario_data in data["answers"].values():
        covenants.update(scenario_data.keys())

    return scenarios, covenants


_SCENARIOS, _COVENANT_NUMBERS = _load_scenarios_and_covenants()


# Веса формулы скоринга (CASE.ru.md) — исключить из запрещённых литералов.
# 0.0 и 0.00 тоже исключены (инициализация/сравнение, не порог).
_WEIGHT_SCORES = {"0.50", "0.30", "0.20", "0.05", "0.5", "0.3", "0.2", "0.0", "0.00"}

# Категории таксономии (template.py): формат-заглушка разрешён в solution/
_TAXONOMY_CATEGORIES = {
    "REVENUE",
    "OTHER_OPEX",
    "INTEREST",
    "PAYROLL",
    "UTILITIES",
    "CAPEX",
    "ALL",
    "FINANCING",
    "RENT",
    "TAX",
}


def _extract_number_formats(num: Decimal | int | float) -> set[str]:
    """Числовой порог во все возможные строковые представления.

    Форматы 9.00, 9.0, 500000, 500_000, 4_000_000. Голые целые добавляются
    только при > 999 — иначе ложные срабатывания на маленьких числах.
    ROUND_HALF_UP — как в score.py. Округлённые результаты, совпавшие с
    весами скоринга, пропускаются.
    """
    formats = set()

    # Decimal-варианты для дробных порогов
    if isinstance(num, (int, float)):  # noqa: UP038
        d = Decimal(str(num))
    else:
        d = num

    # Формат с двумя знаками (ROUND_HALF_UP — как в score.py)
    quantized = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    quantized_str = str(quantized)
    formats.add(quantized_str)

    # Формат с одним знаком
    quantized_one = d.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    quantized_one_str = str(quantized_one)
    # Пропускаем веса скоринга и совпадения с более точной формой
    if quantized_one_str not in {"0.0"} and quantized_one_str != quantized_str:
        formats.add(quantized_one_str)

    # Для целых: формат с подчёркиваниями-разрядами (500_000, 4_000_000)
    if d == d.to_integral_value():
        int_val = int(d)
        int_str = str(int_val)

        # Голое целое — только для больших чисел (> 999)
        if int_val > 999:
            formats.add(int_str)
            # Формат с подчёркиваниями
            if len(int_str) > 3:
                delimited = "_".join([int_str[max(0, i - 3) : i] for i in range(len(int_str), 0, -3)][::-1])
                formats.add(delimited)
        elif int_val > 0:
            # Маленькие целые — добавлять, только если явно записаны (напр. "3.0" из Decimal)
            pass

    return formats


def _extract_tokens(phrase: str) -> set[str]:
    """Токены-слова из фразы (длина >= 4), без общих суффиксов.

    Ловит вложенные имена заёмщиков/контрагентов. Общие корпоративные
    суффиксы (Capital, Group, Holding, Partners, LLP и т.п.) исключены —
    иначе ложные срабатывания, когда тот же суффикс встречается в шаблонах
    ковенантов.
    """
    # Общие корпоративные суффиксы, встречающиеся во многих именах и шаблонах
    generic_suffixes = {
        "Capital",
        "Group",
        "Holding",
        "Partners",
        "LLC",
        "LLP",
        "Inc",
        "Corp",
        "Limited",
        "Services",
        "Bureau",
    }

    tokens = re.findall(r"\b\w{4,}\b", phrase)
    # Оставляем только токены, которые не общие суффиксы
    return {t for t in tokens if t not in generic_suffixes}


def forbidden_literals() -> list[str]:
    """Полный список запрещённых подстрок из eval-данных.

    Источники:
    - Related parties из FACTS (имена + токены 4+ символов)
    - Номера пунктов из шаблона (6.1, 6.2, 6.3)
    - Пороги из SPECS (числовые форматы)
    - ID сценариев (P1-P10, B1, B4)
    - Префиксы TXN-, ACC-
    """
    forbidden = set()

    # Related parties и их словесные токены
    for scenario_facts in FACTS.values():
        for party_name in scenario_facts.get("related_parties", []):
            forbidden.add(party_name)
            # Токены (4+ символов) из имён контрагентов
            forbidden.update(_extract_tokens(party_name))

    # Номера пунктов
    forbidden.update(_COVENANT_NUMBERS)

    # Пороги из SPECS (числа во всех форматах)
    for scenario_specs in SPECS.values():
        for _covenant_id, spec in scenario_specs.items():
            # spec — кортеж: (metric_name, direction, threshold, [optional_dict])
            # Пороги-веса НЕ фильтруются на входе (ревью PR #9, 9-я волна:
            # этот фильтр нейтрализовал узкое исключение по score.py в scan()
            # и оставлял гейт слепым к трём реальным ковенантам) — коллизия
            # с весами разрешается в scan() адресно.
            forbidden.update(_extract_number_formats(spec[2]))

            # Опциональный словарь триггера (например, trigger_financing)
            if len(spec) > 3 and isinstance(spec[3], dict):
                for trigger_val in spec[3].values():
                    forbidden.update(_extract_number_formats(trigger_val))

    # ID сценариев (по границам слова, чтобы PAGE1 не совпал с P1)
    forbidden.update(_SCENARIOS)

    # Префиксы транзакций и счетов
    forbidden.add("TXN-")
    forbidden.add("ACC-")

    # Убираем категории таксономии (разрешены в solution/)
    forbidden.difference_update(_TAXONOMY_CATEGORIES)

    return sorted(forbidden)


def scan(paths: list[Path]) -> list[dict]:
    """Сканирует файлы на запрещённые литералы; возвращает {"file", "line", "literal"}.

    Для ID сценариев — сопоставление по границам слова, иначе ложные срабатывания.
    """
    forbidden = forbidden_literals()
    results = []
    # Пороги, совпавшие с ВЕСАМИ скоринга (0.30/0.20/0.05...), раньше глушились
    # ГЛОБАЛЬНО — гейт был слеп к трём реальным ковенантам (ревью PR #9, 8-я
    # волна). Теперь они запрещены везде, кроме единственного легитимного дома
    # весов — solution/score.py (узкое, задокументированное исключение; НЕ
    # общий allowlist-механизм).
    weight_home = str(Path("solution") / "score.py")

    # Строим паттерны: границы слова для ID сценариев, простая подстрока для остального
    patterns = {}
    for lit in forbidden:
        if lit in _SCENARIOS:
            # Границы слова для ID сценариев
            patterns[lit] = (re.compile(r"\b" + re.escape(lit) + r"\b"), True)
        else:
            # Простой поиск подстроки
            patterns[lit] = (None, False)

    for path in paths:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        path_str = str(path)
        lines = content.splitlines()
        for line_num, line in enumerate(lines, 1):
            for lit, (regex, use_regex) in patterns.items():
                if lit in _WEIGHT_SCORES and path_str.endswith(weight_home):
                    continue
                if use_regex:
                    if regex.search(line):
                        results.append({"file": path_str, "line": line_num, "literal": lit})
                else:
                    if lit in line:
                        results.append({"file": path_str, "line": line_num, "literal": lit})

    return results


def main() -> None:
    """Сканирует solution/*.py и run.sh; exit(1), если найдены нарушения."""
    paths = sorted(Path("solution").glob("*.py")) + [Path("run.sh")]
    paths = [p for p in paths if p.exists()]

    hits = scan(paths)

    if hits:
        print("Grep gate violations found:", file=sys.stderr)
        for hit in hits:
            print(f"{hit['file']}:{hit['line']}: {hit['literal']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
