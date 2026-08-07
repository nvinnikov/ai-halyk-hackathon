"""Греп-гейт (раздел 9 спеки): секунда на прогон, ловит больше, чем половина
мутаций.

Ни одного имени заёмщика или контрагента, номера пункта, порогового числа и
префикса TXN-/ACC- в solution/ — такое знание обязано приходить слоем
извлечения, а не литералом. Список запрещённого строится из eval-данных, а не
пишется руками: добавили заёмщика в эталон — гейт расширился сам.

Находка гейта в solution/ — это находка, а не ложное срабатывание: чинится код,
а не гейт. Исключения ниже перечислены поимённо и с причиной.
"""

import ast
import io
import json
import re
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, "solution")
sys.path.insert(0, "eval")

from expected_extraction import FACTS, SPECS

from util import ROOT

# Веса официальной формулы скоринга (CASE, раздел 4) стоят в solution/score.py
# по определению формулы и порогами ковенантов не являются. Они попадают в
# SPECS как обычные числа (0.20 — порог insurance_cover, 0.30 — порог
# tax_utility_to_ebitda), поэтому вычитаются из запрещённых явно.
_SCORING_WEIGHTS = {"0.50", "0.5", "0.30", "0.3", "0.20", "0.2", "0.05"}

# Токены имён, которые являются обычными английскими словами и встречаются в
# коде как термины предметной области, а не как знание о конкретном заёмщике.
_GENERIC_NAME_TOKENS = {
    "capital",  # CAPEX-терминология
    "group",  # «группа» в смысле консолидации, не имя
    "holding",
    "holdings",
    "partners",
    "services",
    "bureau",
    "advisory",
    "processing",
    "distributors",
    "fuel",
    "risk",
    "engineering",
}

_MIN_TOKEN_LEN = 4


def _number_forms(value) -> set[str]:
    """Одно и то же число в записях, которыми его пишут в коде и в промптах.

    Пробельные разделители тысяч обязательны: `$4 000 000` в тексте промпта —
    это ровно тот порог, который иначе проползает мимо гейта, потому что
    русская типографика пишет деньги пробелом, а не запятой и не подчёркиванием.
    """
    if isinstance(value, int):
        grouped = f"{value:,}"
        return {
            str(value),
            f"{value:_}",
            grouped,
            grouped.replace(",", " "),
            grouped.replace(",", "\xa0"),
            grouped.replace(",", " "),
        }
    return {f"{value:.2f}", repr(float(value))}


def _names() -> set[str]:
    """Имена контрагентов, дочек и связанных сторон из эталона."""
    out: set[str] = set()
    for facts in FACTS.values():
        out.update(facts.get("related_parties", ()))
        out.update(facts.get("unrestricted_subsidiaries", ()))
        for rc in facts.get("reclass", ()):
            if rc.get("counterparty"):
                out.add(rc["counterparty"])
    return out


def _template() -> dict:
    templates = sorted((ROOT / "dataset").rglob("submission_template.json"))
    assert templates, "публичный submission_template.json не найден"
    return json.loads(templates[0].read_text())


def forbidden_literals() -> list[str]:
    lits: set[str] = {"TXN-", "ACC-"}

    names = _names()
    lits |= names
    for name in names:
        for token in re.findall(r"[A-Za-z]+", name):
            if len(token) >= _MIN_TOKEN_LEN and token.lower() not in _GENERIC_NAME_TOKENS:
                lits.add(token)

    template = _template()
    lits |= set(template["answers"])  # идентификаторы сценариев
    for cells in template["answers"].values():
        lits |= set(cells)  # номера пунктов

    for clauses in SPECS.values():
        for spec in clauses.values():
            lits |= _number_forms(spec[2])
            if len(spec) > 3:
                for extra in spec[3].values():
                    lits |= _number_forms(extra)

    return sorted(lits - _SCORING_WEIGHTS)


def _word_like(literal: str) -> bool:
    """Идентификаторы сценариев и голые токены имён ищутся как отдельные слова:
    иначе `P1` ловится внутри `PAGE1`, а `Aral` — внутри `Parallel`."""
    return bool(re.fullmatch(r"[A-Za-z]+\d*", literal))


def _code_lines(path: Path) -> list[str]:
    """Строки файла с вычтенными комментариями и докстрингами, номера строк
    сохранены (вычтенное заменяется пробелами).

    Гейт стережёт знание, зашитое **мимо слоя извлечения**, то есть влияющее на
    поведение: выражение в коде, ключ, регулярку и — главное — текст промпта,
    который уходит в модель. Комментарий и докстринг адресованы человеку, в
    модель не попадают и вычислением не читаются; оставить их под гейтом
    значило бы запретить объяснять в коде, почему он такой, ради нулевой
    защиты. Промпты при этом остаются под гейтом полностью: они живут в
    обычных строковых литералах, а не в докстрингах.
    """
    src = path.read_text()
    lines = src.splitlines()
    if path.suffix != ".py":
        return [ln.split("#", 1)[0] for ln in lines]

    grid = [list(ln) for ln in lines]

    def blank(l1: int, c1: int, l2: int, c2: int) -> None:
        for i in range(l1 - 1, min(l2, len(grid))):
            row = grid[i]
            start = c1 if i == l1 - 1 else 0
            end = c2 if i == l2 - 1 else len(row)
            for j in range(start, min(end, len(row))):
                row[j] = " "

    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            blank(tok.start[0], tok.start[1], tok.end[0], tok.end[1])

    doc_owners = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, doc_owners) or not ast.get_docstring(node):
            continue
        lit = node.body[0].value  # type: ignore[attr-defined]
        blank(lit.lineno, lit.col_offset, lit.end_lineno, lit.end_col_offset)

    return ["".join(row) for row in grid]


def scan(paths: list[Path]) -> list[dict]:
    literals = forbidden_literals()
    patterns = [(lit, re.compile(rf"\b{re.escape(lit)}\b") if _word_like(lit) else None) for lit in literals]
    hits = []
    for path in paths:
        if not path.exists():
            continue
        for n, line in enumerate(_code_lines(path), start=1):
            for lit, rx in patterns:
                found = rx.search(line) if rx else (lit in line)
                if found:
                    hits.append({"file": str(path), "line": n, "literal": lit})
    return sorted(hits, key=lambda h: (h["file"], h["line"], h["literal"]))


def main() -> int:
    files = sorted((ROOT / "solution").glob("*.py")) + [ROOT / "run.sh"]
    hits = scan(files)
    for h in hits:
        print(f"{h['file']}:{h['line']}: запрещённый литерал {h['literal']!r}")
    if hits:
        print(f"\nгреп-гейт: {len(hits)} утечек знания мимо слоя извлечения")
        return 1
    print(f"греп-гейт чист: {len(files)} файлов")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
