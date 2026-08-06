"""Экстракционный eval (7.1): восстанавливает ли LLM-слой эталон из PDF.

Меряет ровно ту часть, которой раньше не существовало и которая провалилась
бы 9 августа. Главный инструмент разбора просадок 8 августа.
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "solution")
sys.path.insert(0, "eval")

from expected_extraction import FACTS, SPECS

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
    """
    out = []

    # related_parties — проверяем только если в want
    for field in ("related_parties", "unrestricted_subsidiaries"):
        if field in want:
            g, w = _name_keys(got.get(field, [])), _name_keys(want.get(field, []))
            if g != w:
                out.append(f"{field}: got {sorted(map(sorted, g))} != want {sorted(map(sorted, w))}")

    # reclass — сравниваем как множество кортежей (txn, counterparty, to)
    if "reclass" in want:
        g_rc = {
            (
                rc.get("txn"),
                frozenset(tokens(rc["counterparty"])) if rc.get("counterparty") else None,
                rc["to"],
            )
            for rc in got.get("reclass", [])
        }
        w_rc = {
            (
                rc.get("txn"),
                frozenset(tokens(rc["counterparty"])) if rc.get("counterparty") else None,
                rc["to"],
            )
            for rc in want.get("reclass", [])
        }
        if g_rc != w_rc:
            out.append(f"reclass: got {sorted(map(str, g_rc))} != want {sorted(map(str, w_rc))}")

    # exclude
    if "exclude" in want:
        if sorted(got.get("exclude", [])) != sorted(want.get("exclude", [])):
            out.append("exclude: расходятся")

    # amount_override
    if "amount_override" in want:
        g_ov = {k: Decimal(str(v)) for k, v in got.get("amount_override", {}).items()}
        w_ov = {k: Decimal(str(v)) for k, v in want.get("amount_override", {}).items()}
        if g_ov != w_ov:
            out.append("amount_override: расходятся")

    # fx_rates — по множеству пар (currency, usd_per_unit) с допуском 1e-4 на курс
    if "fx_rates" in want:
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

        # Проверяем, что нет лишних в extracted
        for g_curr, g_rate in sorted(g_fx):
            found = False
            for w_curr, w_rate in w_fx:
                if w_curr == g_curr and abs(g_rate - w_rate) <= tolerance:
                    found = True
                    break
            if not found:
                out.append(f"fx_rates: extra currency {g_curr} usd_per_unit {g_rate} in got")

    # ebitda_addbacks — как мультимножества Decimal с допуском 0.01
    if "ebitda_addbacks" in want:
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

        # Сортируем для сравнения мультимножеств
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

    # addback_materiality — точное сравнение
    if "addback_materiality" in want:
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

    # doc_facts (severance_liability и др.)
    # Проверяем наличие severance_liability, если она в want
    if "severance_liability" in want:
        g = got.get("doc_facts", {}).get("severance_liability")
        if g is None or abs(Decimal(str(g)) - Decimal(str(want["severance_liability"]))) > Decimal("0.01"):
            out.append(f"doc_facts.severance_liability: got {g} != want {want['severance_liability']}")

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

        # Проверяем direction
        if sp["direction"] != direction:
            out.append(f"{cl}: direction {sp['direction']} != {direction}")

        # Проверяем limit (точное сравнение после нормализации в Decimal)
        if abs(Decimal(sp["limit"]) - Decimal(str(limit))) > Decimal("1E-9"):
            out.append(f"{cl}: limit {sp['limit']} != {limit}")

        # Проверяем template (если он есть и не None)
        if sp.get("template") and sp["template"] != name:
            out.append(f"{cl}: шаблон {sp['template']} != {name}")

    return out


def main(archive: Path) -> int:
    """Запусти экстракционный eval и выведи отчёт по всем заёмщикам."""
    from ledger import extract_archive
    from util import workdir

    ds_hash, _ = extract_archive(archive)
    wd = workdir(ds_hash)
    index = json.loads((wd / "index.json").read_text())
    bad = 0
    for sc in sorted(FACTS):
        acc = index["scenario_to_account"].get(sc)
        facts = json.loads((wd / "facts" / f"{acc}.json").read_text())
        specs = json.loads((wd / "specs" / f"{acc}.json").read_text())["clauses"]
        df, ds = diff_facts(facts, FACTS[sc]), diff_specs(specs, SPECS[sc])
        bad += len(df) + len(ds)
        status = "OK" if not (df or ds) else "  ".join(df + ds) + "  <<<"
        print(f"{sc:<4} {status}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
