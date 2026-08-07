"""Офлайн-юниты extracted-режима solve: чистые функции сопоставления/сборки
cellspec и fail-open вокруг документного конвейера (задача 24, ревью раунда 1).

Полный прогон на LLM — в tests/test_extracted_run.py (маркер llm). Здесь —
то, что должен ловить make check без ключа: регресс в _match_clauses,
_extracted_cellspec и в деградации при сбое build_dossiers/_extracted_inputs.
"""

import json
from decimal import Decimal
from pathlib import Path

from mutations import _isolated_solve_out

import solve
from dsl import parse
from ledger import extract_archive, find_inputs, load_ledger, rows_of
from scindex import INDEX_VERSION, build_index
from stages import artifact
from templates import TEMPLATES, title_key
from util import workdir

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")


# --- _match_clauses -----------------------------------------------------------


def test_match_clauses_exact_match():
    mapping, unmatched = solve._match_clauses(["6.1", "6.2", "6.3"], ["6.1", "6.2", "6.3"])
    assert mapping == {"6.1": "6.1", "6.2": "6.2", "6.3": "6.3"}
    assert unmatched == []


def test_match_clauses_suffix_fallback_when_counts_equal():
    # Номера пунктов не совпадают целиком (другой раздел договора), но число
    # ячеек равно числу извлечённых пунктов — доматч по числовому суффиксу.
    mapping, unmatched = solve._match_clauses(["6.1", "6.2", "6.3"], ["7.1", "7.2", "7.3"])
    assert mapping == {"6.1": "7.1", "6.2": "7.2", "6.3": "7.3"}
    assert unmatched == []


def test_match_clauses_mixed_direct_and_suffix():
    # 6.1 матчится напрямую, 6.2/6.3 — по суффиксу с оставшимися пунктами.
    mapping, unmatched = solve._match_clauses(["6.1", "6.2", "6.3"], ["6.1", "7.2", "7.3"])
    assert mapping == {"6.1": "6.1", "6.2": "7.2", "6.3": "7.3"}
    assert unmatched == []


def test_match_clauses_no_suffix_fallback_when_counts_differ():
    # Число ячеек шаблона (3) не равно числу извлечённых пунктов (2) —
    # доматч по суффиксу не пытается, непокрытые ячейки уходят в unmatched.
    mapping, unmatched = solve._match_clauses(["6.1", "6.2", "6.3"], ["7.1", "7.2"])
    assert mapping == {}
    assert sorted(unmatched) == ["6.1", "6.2", "6.3"]


def test_match_clauses_ambiguous_suffix_stays_unmatched():
    # Оба извлечённых пункта имеют суффикс "1" — неоднозначность, обе целевые
    # ячейки остаются непокрытыми (алярм clause_unmatched выше по стеку).
    mapping, unmatched = solve._match_clauses(["6.1", "6.2"], ["7.1", "8.1"])
    assert mapping == {}
    assert sorted(unmatched) == ["6.1", "6.2"]


# --- _extracted_cellspec -------------------------------------------------------


def _spec(**over) -> dict:
    base = {
        "quote": "цитата пункта",
        "valid": True,
        "errors": [],
        "missing_doc_keys": [],
        "title_key": "неизвестный заголовок без совпадения",
        "template": None,
        "direction": "max",
        "limit": "100",
        "trigger": None,
        "metric": "agg(CAPEX, out)",
    }
    base.update(over)
    return base


def test_extracted_cellspec_missing_clause_is_lookup_error():
    cellspec_or_error, quote = solve._extracted_cellspec(None, "6.1")
    assert isinstance(cellspec_or_error, LookupError)
    assert quote == ""


def test_extracted_cellspec_invalid_spec_is_value_error_but_keeps_quote():
    sp = _spec(valid=False, errors=["quote_unverified"])
    cellspec_or_error, quote = solve._extracted_cellspec(sp, "6.1")
    assert isinstance(cellspec_or_error, ValueError)
    assert quote == sp["quote"]  # лестница эвристики читает цитату даже у невалидной спеки


def test_extracted_cellspec_heading_match_wins_over_template():
    # title_key совпадает с заголовком шаблона capex — исполняется
    # канонический DSL шаблона, даже если specs_extract матчил другую
    # сигнатуру (искусственно испорчена, чтобы отличить источник).
    heading = title_key("Максимальные расходы по категории")
    sp = _spec(title_key=heading, template="revenue", metric="agg(TAX, out)")
    cellspec, quote = solve._extracted_cellspec(sp, "6.1")
    assert isinstance(cellspec, dict)
    assert cellspec["metric_text"] == TEMPLATES["capex"]
    assert quote == sp["quote"]


def test_extracted_cellspec_falls_back_to_template_signature():
    # Заголовок не совпал ни с одним шаблоном, но специфика уже посчитала
    # сигнатурный матч (match_signature) в sp["template"].
    sp = _spec(template="capex", metric="agg(CAPEX, out)")
    cellspec, _quote = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["metric_text"] == TEMPLATES["capex"]


def test_extracted_cellspec_falls_back_to_raw_metric():
    # Ни заголовок, ни сигнатура не совпали — исполняется сырой DSL спеки.
    raw_metric = "agg(TAX, out, min_amount(10))"
    sp = _spec(template=None, metric=raw_metric)
    cellspec, _quote = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["metric_text"] == raw_metric
    assert cellspec["metric_ast"] == parse(raw_metric)


def test_extracted_cellspec_malformed_limit_returns_exception_not_raises():
    sp = _spec(limit="not-a-number")
    cellspec_or_error, quote = solve._extracted_cellspec(sp, "6.1")
    assert isinstance(cellspec_or_error, Exception)
    assert not isinstance(cellspec_or_error, dict)
    assert quote == sp["quote"]


def test_extracted_cellspec_uses_decimal_limit_and_trigger():
    sp = _spec(limit="1.70", trigger="gt(agg(FINANCING, in), const(4000000))")
    cellspec, _quote = solve._extracted_cellspec(sp, "6.1")
    assert cellspec["limit"] == Decimal("1.70")
    assert cellspec["trigger_ast"] is not None


# --- fail-open: сбой документного конвейера не роняет прогон ------------------


def _extracted_context() -> tuple[Path, Path, dict, list[str]]:
    """Тот же набор входов, что main() строит до документного конвейера —
    воспроизведён здесь, чтобы дёргать _extracted_inputs напрямую, в обход
    полного main()."""
    ds_hash, input_dir = extract_archive(PUBLIC_ZIP)
    wd = workdir(ds_hash)
    inputs = find_inputs(input_dir)
    template = json.loads(inputs["template"].read_text())
    targets = sorted(template["answers"])
    ledger_art = load_ledger(wd, input_dir, target_scenarios=targets)
    all_rows = rows_of(ledger_art)
    index = artifact(wd / "index.json", INDEX_VERSION, lambda: build_index(all_rows, targets))
    return wd, input_dir, index, targets


def test_dossier_build_failure_is_fail_open(monkeypatch):
    """Critical (ревью раунда 1): build_dossiers падает целиком — раньше это
    роняло _extracted_inputs, а с ним и main(), до записи скелета."""
    wd, input_dir, index, targets = _extracted_context()

    def boom(*a, **k):
        raise RuntimeError("искусственный сбой сшивки досье")

    monkeypatch.setattr(solve, "build_dossiers", boom)
    facts_by_sc, specs_by_sc = solve._extracted_inputs(wd, input_dir, index, targets)

    assert set(facts_by_sc) == set(targets)
    assert set(specs_by_sc) == set(targets)
    for sc in targets:
        assert specs_by_sc[sc]["clauses"] == {}
        assert any(a["kind"] == "dossier_build_failed" for a in specs_by_sc[sc]["alarms"])
        assert "doc_facts" in facts_by_sc[sc]  # факты прошли через _with_doc_facts


def test_extracted_inputs_failure_does_not_kill_main(monkeypatch):
    """Второй рубеж: даже если _extracted_inputs целиком отказала (баг, а не
    ожидаемый сбой документа) — main() дописывает submission по лестнице,
    а не падает с ненулевым кодом."""

    def boom(*a, **k):
        raise RuntimeError("искусственный сбой документного конвейера")

    monkeypatch.setattr(solve, "_extracted_inputs", boom)
    with _isolated_solve_out(PUBLIC_ZIP):  # без подмены solve.OUT тест писал бы поверх боевого out/
        answers = solve.main(PUBLIC_ZIP, facts_source="extracted")
    for sc, cells in answers.items():
        for clause, cell in cells.items():
            assert sorted(cell) == ["actual", "evidence_txn_id", "status"], f"{sc} {clause}: {cell}"
            assert cell["status"] in ("BREACH", "COMPLIANT")
            assert isinstance(cell["actual"], int | float)
