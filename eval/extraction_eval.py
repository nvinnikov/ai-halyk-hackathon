"""Экстракционный eval (7.1): восстанавливает ли LLM-слой эталон из PDF.

Меряет ровно ту часть, которой раньше не существовало и которая провалилась
бы 9 августа. Главный инструмент разбора просадок 8 августа.
"""

import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, "solution")
sys.path.insert(0, "eval")

from expected_extraction import FACTS, SPECS

import solve
from engine import tokens


def _name_keys(names):
    """Преобразуй имена в нормализованные токены."""
    return {tokens(n) for n in names}


def diff_facts(got: dict, want: dict) -> list[str]:
    """Сравни извлечённые факты с эталоном.

    Имена сравниваются токенами (нормализация), числа — точно (Decimal).
    fx_rates сравниваются по множеству пар (currency, usd_per_unit) с допуском 1e-4.
    ebitda_addbacks сравниваются как мультимножества Decimal с допуском 0.01.
    addback_materiality сравнивается точно.

    Сравнение выполняется ВСЕГДА для базовых полей (related_parties, reclass, exclude,
    amount_override, fx_rates) с дефолтными пустыми значениями, чтобы поймать галлюцинации
    extraction-слоя (выписанное поле при отсутствии его в эталоне). Скаляры (addback_materiality,
    severance_liability) гардятся, чтобы избежать краша при вычитании из None.
    """
    out = []

    # related_parties / unrestricted_subsidiaries — сравниваем всегда
    for field in ("related_parties", "unrestricted_subsidiaries"):
        g, w = _name_keys(got.get(field, [])), _name_keys(want.get(field, []))
        if g != w:
            out.append(f"{field}: got {sorted(map(sorted, g))} != want {sorted(map(sorted, w))}")

    # reclass — сравниваем как множество кортежей (txn, counterparty, to)
    # tokens() уже возвращает frozenset, не оборачиваем повторно
    g_rc = {
        (rc.get("txn"), tokens(rc["counterparty"]) if rc.get("counterparty") else None, rc["to"])
        for rc in got.get("reclass", [])
    }
    w_rc = {
        (rc.get("txn"), tokens(rc["counterparty"]) if rc.get("counterparty") else None, rc["to"])
        for rc in want.get("reclass", [])
    }
    if g_rc != w_rc:
        out.append(f"reclass: got {sorted(map(str, g_rc))} != want {sorted(map(str, w_rc))}")

    # exclude — сравниваем всегда
    if sorted(got.get("exclude", [])) != sorted(want.get("exclude", [])):
        out.append("exclude: расходятся")

    # amount_override — сравниваем всегда
    g_ov = {k: Decimal(str(v)) for k, v in got.get("amount_override", {}).items()}
    w_ov = {k: Decimal(str(v)) for k, v in want.get("amount_override", {}).items()}
    if g_ov != w_ov:
        out.append("amount_override: расходятся")

    # fx_rates — по множеству пар (currency, usd_per_unit) с допуском 1e-4 на курс
    # Сравниваем всегда
    g_fx = set()
    for fx in got.get("fx_rates", []):
        try:
            rate = Decimal(str(fx["usd_per_unit"]))
            g_fx.add((fx["currency"], rate))
        except (KeyError, ValueError):
            pass

    w_fx = set()
    for fx in want.get("fx_rates", []):
        try:
            rate = Decimal(str(fx["usd_per_unit"]))
            w_fx.add((fx["currency"], rate))
        except (KeyError, ValueError):
            pass

    # Проверяем, что все expected fx_rates покрыты (с допуском)
    tolerance = Decimal("1e-4")
    for w_curr, w_rate in sorted(w_fx):
        found = False
        for g_curr, g_rate in g_fx:
            if g_curr == w_curr and abs(g_rate - w_rate) <= tolerance:
                found = True
                break
        if not found:
            out.append(
                f"fx_rates: currency {w_curr} usd_per_unit {w_rate} not found in got with tolerance 1e-4"
            )

    # Проверяем, что нет лишних в extracted (по валютам, не по конкретным парам)
    g_currencies = {curr for curr, _ in g_fx}
    w_currencies = {curr for curr, _ in w_fx}
    for g_curr in sorted(g_currencies - w_currencies):
        # Выписана валюта, которой нет в want
        for g_rate in sorted([r for c, r in g_fx if c == g_curr]):
            out.append(f"fx_rates: extra currency {g_curr} usd_per_unit {g_rate} in got")

    # ebitda_addbacks — как мультимножества Decimal с допуском 0.01
    # Сравниваем всегда
    g_addbacks = []
    for v in got.get("ebitda_addbacks", []):
        try:
            g_addbacks.append(Decimal(str(v)))
        except (ValueError, TypeError):
            pass

    w_addbacks = []
    for v in want.get("ebitda_addbacks", []):
        try:
            w_addbacks.append(Decimal(str(v)))
        except (ValueError, TypeError):
            pass

    # Сортируем для детерминированного сравнения мультимножеств
    g_addbacks.sort()
    w_addbacks.sort()

    if len(g_addbacks) != len(w_addbacks):
        out.append(f"ebitda_addbacks: got {len(g_addbacks)} items != want {len(w_addbacks)} items")
    else:
        tolerance = Decimal("0.01")
        mismatches = []
        for i, (g_val, w_val) in enumerate(zip(g_addbacks, w_addbacks, strict=True)):
            if abs(g_val - w_val) > tolerance:
                mismatches.append(f"[{i}] got {g_val} != want {w_val}")
        if mismatches:
            out.append(f"ebitda_addbacks: {', '.join(mismatches)}")

    # addback_materiality — нормализация для дефолта "0" из _empty_facts
    # Если want.ebitda_addbacks пусто/отсутствует, то got.addback_materiality может быть дефолтным "0"
    # (не ошибка); если got содержит ненулевое значение без эталона addbacks — это галлюцинация.
    w_has_addbacks = bool(want.get("ebitda_addbacks"))
    if w_has_addbacks:
        # Есть addbacks в want — проверяем materiality точно
        g_mat = got.get("addback_materiality")
        w_mat = want.get("addback_materiality")
        if g_mat is not None and w_mat is not None:
            try:
                g_d = Decimal(str(g_mat))
                w_d = Decimal(str(w_mat))
                if g_d != w_d:
                    out.append(f"addback_materiality: got {g_mat} != want {w_mat}")
            except (ValueError, TypeError):
                if g_mat != w_mat:
                    out.append(f"addback_materiality: got {g_mat} != want {w_mat}")
        elif (g_mat is None) != (w_mat is None):
            out.append(f"addback_materiality: got {g_mat} != want {w_mat}")
    else:
        # Нет addbacks в want — materiality должен быть дефолтным (нулевым)
        g_mat = got.get("addback_materiality", "0")
        try:
            g_d = Decimal(str(g_mat))
            if g_d != Decimal("0"):
                out.append(f"addback_materiality: got {g_mat} != want 0 (no addbacks expected)")
        except (ValueError, TypeError):
            if g_mat not in ("0", None, ""):
                out.append(f"addback_materiality: got {g_mat} != want 0 (no addbacks expected)")

    # doc_facts.severance_liability — точное сравнение (гардим, т.к. скаляр)
    # Проверяем наличие severance_liability в want перед вычитанием
    if "severance_liability" in want:
        g = got.get("doc_facts", {}).get("severance_liability")
        if g is None or abs(Decimal(str(g)) - Decimal(str(want["severance_liability"]))) > Decimal("0.01"):
            out.append(f"doc_facts.severance_liability: got {g} != want {want['severance_liability']}")

    # Эталонные doc_facts целиком, а не поимённо: иначе каждый новый ключ
    # эталона появляется без измерения — так и вышло с group_capex, который
    # приехал в эталон вместе с целым новым LLM-проходом и не мерился ничем
    # (ревью PR #23, вторая волна). severance_liability выше оставлен своим
    # блоком: он живёт в want плоским ключом, а не внутри doc_facts.
    for key, wanted in sorted(want.get("doc_facts", {}).items()):
        g = got.get("doc_facts", {}).get(key)
        try:
            ok = g is not None and abs(Decimal(str(g)) - Decimal(str(wanted))) <= Decimal("0.01")
        except (ValueError, TypeError, InvalidOperation):
            ok = str(g) == str(wanted)
        if not ok:
            out.append(f"doc_facts.{key}: got {g} != want {wanted}")

    return out


def diff_specs(got_clauses: dict, want_specs: dict) -> list[str]:
    """Сравни извлечённые спеки с эталоном.

    got_clauses[clause] = {direction, limit, template, valid, ...}
    want_specs[clause] = (metric_name, direction, limit) или (metric_name, direction, limit, extra_params)
    """
    out = []
    for cl in sorted(want_specs):
        spec_tuple = want_specs[cl]
        name = spec_tuple[0]
        direction = spec_tuple[1]
        limit = spec_tuple[2]

        sp = got_clauses.get(cl)
        if sp is None:
            out.append(f"{cl}: пункт не извлечён")
            continue

        # Спека, которую solve отвергнет (valid=False → лестница), — расхождение
        # сама по себе: иначе eval показывал бы картину лучше реальной (ревью PR #9).
        if not sp.get("valid", True):
            out.append(f"{cl}: спека невалидна ({sp.get('errors') or sp.get('missing_doc_keys')})")

        # Проверяем direction
        if sp["direction"] != direction:
            out.append(f"{cl}: direction {sp['direction']} != {direction}")

        # Проверяем limit (точное сравнение после нормализации в Decimal).
        # Порог, который _check пропустил как невалидный («5%»), — не число:
        # InvalidOperation ронял бы весь отчёт, теряя разбор по остальным
        # заёмщикам (ревью PR #9, 19-я волна) — фиксируем как расхождение.
        try:
            limit_diverges = abs(Decimal(sp["limit"]) - Decimal(str(limit))) > Decimal("1E-9")
        except InvalidOperation:
            limit_diverges = True
        if limit_diverges:
            out.append(f"{cl}: limit {sp['limit']} != {limit}")

        # Проверяем template (если он есть и не None)
        if sp.get("template") and sp["template"] != name:
            out.append(f"{cl}: шаблон {sp['template']} != {name}")

    return out


def main(archive: Path, wd: Path | None = None) -> int:
    """Запусти экстракционный eval и выведи раздельный отчёт по фактам и спекам.

    Печатает отчёт для каждого заёмщика отдельно по фактам и спекам.
    В конце выводит итоговые проценты: доля заёмщиков без расхождений.
    Формула: good_count / total_count * 100% для каждой категории.
    Возвращает exit code 1 при наличии расхождений, 0 если всё чистое.

    Спеки извлекаются через specs_extract.extract_specs() (как в solve),
    чтобы получить clauses из сырого артефакта covenants.

    wd — рабочий каталог прогона; по умолчанию вычисляется из archive через
    extract_archive/workdir (боевой путь). Параметр существует ради тестируемости:
    регрессионный тест подсовывает заранее собранный каталог с реальной формой
    артефактов, не распаковывая архив.
    """
    from specs_extract import extract_specs

    if wd is None:
        from ledger import extract_archive
        from util import workdir

        ds_hash, _ = extract_archive(archive)
        wd = workdir(ds_hash)
    index = json.loads((wd / "index.json").read_text())

    facts_good = 0
    specs_good = 0
    total = len(FACTS)

    for sc in sorted(FACTS):
        acc = index["scenario_to_account"].get(sc)
        facts = json.loads((wd / "facts" / f"{acc}.json").read_text())
        # Читаем dossier и вызываем extract_specs как solve: fact_keys — от
        # обогащённых фактов, производные ключи видимы (ревью PR #9, 4-я волна)
        dossier = json.loads((wd / "dossier" / f"{acc}.json").read_text())
        spec_art = extract_specs(wd, dossier, set(solve._with_doc_facts(facts)["doc_facts"]))
        specs = spec_art.get("clauses", {})

        df, ds = diff_facts(facts, FACTS[sc]), diff_specs(specs, SPECS[sc])

        # Печатаем факты
        if df:
            print(f"{sc:<4} facts: " + "  ".join(df))
        else:
            print(f"{sc:<4} facts: OK")
            facts_good += 1

        # Печатаем спеки
        if ds:
            print(f"{sc:<4} specs: " + "  ".join(ds))
        else:
            print(f"{sc:<4} specs: OK")
            specs_good += 1

    # Итоговые проценты
    facts_pct = (facts_good / total * 100) if total > 0 else 0
    specs_pct = (specs_good / total * 100) if total > 0 else 0
    summary = (
        f"\nSummary: facts {facts_good}/{total} ({facts_pct:.1f}%), "
        f"specs {specs_good}/{total} ({specs_pct:.1f}%)"
    )
    print(summary)

    return 1 if (facts_good < total or specs_good < total) else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
