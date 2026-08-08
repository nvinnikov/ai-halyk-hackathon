"""Офлайн-юниты extracted-режима solve: чистые функции сопоставления/сборки
cellspec и fail-open вокруг документного конвейера (задача 24, ревью раунда 1).

Полный прогон на LLM — в tests/test_extracted_run.py (маркер llm). Здесь —
то, что должен ловить make check без ключа: регресс в _match_clauses,
_extracted_cellspec и в деградации при сбое build_dossiers/_extracted_inputs.
"""

import json
from decimal import Decimal
from pathlib import Path

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


def test_match_clauses_suffix_fallback_survives_extra_covenant():
    # Ревью PR #9 (3-я волна): лишний извлечённый ковенант (промпт просит
    # «найди ВСЕ») не выключает доматч — от ложного матча защищает
    # однозначность суффикса, а не равенство счётчиков.
    mapping, unmatched = solve._match_clauses(["6.1", "6.2", "6.3"], ["7.1", "7.2", "7.3", "9.5"])
    assert mapping == {"6.1": "7.1", "6.2": "7.2", "6.3": "7.3"}
    assert unmatched == []


def test_match_clauses_missing_covenant_leaves_cell_unmatched():
    # Извлечено меньше, чем ячеек: непокрытая ячейка уходит в unmatched
    # (лестница), покрытые матчатся по однозначным суффиксам.
    mapping, unmatched = solve._match_clauses(["6.1", "6.2", "6.3"], ["7.1", "7.2"])
    assert mapping == {"6.1": "7.1", "6.2": "7.2"}
    assert unmatched == ["6.3"]


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
    # Категория извлечённой метрики совпадает с шаблонной: категорийное
    # расхождение — отдельный путь с откатом (тест ниже).
    heading = title_key("Максимальные расходы по категории")
    sp = _spec(title_key=heading, template="revenue", metric="agg(CAPEX, out, min_amount(10))")
    cellspec, quote = solve._extracted_cellspec(sp, "6.1")
    assert isinstance(cellspec, dict)
    assert cellspec["metric_text"] == TEMPLATES["capex"]
    assert quote == sp["quote"]


def test_extracted_cellspec_category_divergence_keeps_template_with_alarm():
    # Заголовок капекс-шаблона, извлечённая формула — про другую категорию:
    # шаблон всё равно исполняется (на публичном наборе такие расхождения —
    # ошибки извлечения синонимичных категорий, откат стоил −5.0 офлайн),
    # но расхождение обязано быть видно алярмом.
    heading = title_key("Максимальные расходы по категории")
    sp = _spec(title_key=heading, template=None, metric="agg(TAX, out)")
    cellspec, _quote = solve._extracted_cellspec(sp, "6.1")
    assert isinstance(cellspec, dict)
    assert cellspec["metric_text"] == TEMPLATES["capex"]
    kinds = [a["kind"] for a in cellspec["match_alarms"]]
    assert kinds == ["heading_category_divergence"]


def test_with_doc_facts_keeps_model_total_when_no_addbacks():
    # Добавок не извлечено, модель дала итог numeric_fact'ом: ноль поверх
    # извлечённого числа — потеря данных, модельное значение остаётся
    # (ревью PR #9, 25-я волна).
    facts = {"ebitda_addbacks": [], "doc_facts": {"ebitda_addbacks_material_total": "500.00"}}
    out = solve._with_doc_facts(facts)
    assert out["doc_facts"]["ebitda_addbacks_material_total"] == "500.00"


def test_with_doc_facts_arithmetic_wins_when_addbacks_present():
    # Есть из чего считать — арифметика кода перебивает модельное значение.
    facts = {
        "ebitda_addbacks": ["100", "200"],
        "addback_materiality": "150",
        "doc_facts": {"ebitda_addbacks_material_total": "999"},
    }
    out = solve._with_doc_facts(facts)
    assert out["doc_facts"]["ebitda_addbacks_material_total"] == "200"


def test_run_cell_match_alarms_survive_dsl_fallback():
    # Спека с match_alarms есть, вычисление падает (doc-ключа нет) —
    # алярмы подмены не затираются fallback-путём (ревью PR #9, 25-я волна).
    cellspec = {
        "metric_ast": parse("doc(missing_key)"),
        "metric_text": "doc(missing_key)",
        "direction": "max",
        "limit": Decimal("100"),
        "trigger_ast": None,
        "match_alarms": [{"kind": "heading_signature_divergence", "extracted": "x", "template": "y"}],
    }
    _cell, trace = solve.run_cell("SC-Y", "9.8", [], {"doc_facts": {}}, cellspec, [])
    assert trace["tier"] == 2 and "dsl_error" in trace
    assert any(a.get("kind") == "heading_signature_divergence" for a in trace["alarms"])


def test_run_cell_match_alarms_reach_trace_alarms():
    # match_alarms обязаны доехать до общего trace["alarms"] — только его
    # читают _alarm_counts и invariants._collect_report_alarms; scenario и
    # clause внутри словаря спасают от глобального дедупа точных дублей.
    cellspec = {
        "metric_ast": parse("agg(CAPEX, out)"),
        "metric_text": "agg(CAPEX, out)",
        "direction": "max",
        "limit": Decimal("100"),
        "trigger_ast": None,
        "match_alarms": [{"kind": "heading_signature_divergence", "extracted": "x", "template": "y"}],
    }
    _cell, trace = solve.run_cell("SC-X", "9.9", [], {}, cellspec, [])
    got = [a for a in trace["alarms"] if a["kind"] == "heading_signature_divergence"]
    assert got and got[0]["scenario"] == "SC-X" and got[0]["clause"] == "9.9"


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
    answers = solve.main(PUBLIC_ZIP, facts_source="extracted")
    for sc, cells in answers.items():
        for clause, cell in cells.items():
            assert sorted(cell) == ["actual", "evidence_txn_id", "status"], f"{sc} {clause}: {cell}"
            assert cell["status"] in ("BREACH", "COMPLIANT")
            assert isinstance(cell["actual"], int | float)


def test_specs_failure_keeps_extracted_facts(monkeypatch, tmp_path):
    """Ревью PR #9 (7-я волна): падение стадии спек не обнуляет уже посчитанные
    факты — fx_rates заёмщика остаются в донорском пуле."""
    import facts_extract as fe

    monkeypatch.setattr(solve, "find_inputs", lambda d: {"pdfs": []})
    monkeypatch.setattr(
        solve, "build_dossiers", lambda wd, pdfs, index, all_accounts=None: {"ACC-X": {"account_id": "ACC-X"}}
    )
    good_facts = {**fe._empty_facts(), "fx_rates": [{"currency": "EUR", "usd_per_unit": "1.1"}]}
    monkeypatch.setattr(solve, "extract_facts", lambda wd, d: dict(good_facts))

    def boom(*a, **k):
        raise RuntimeError("specs stage down")

    monkeypatch.setattr(solve, "extract_specs", boom)
    index = {"scenario_to_account": {"S1": "ACC-X"}}
    facts_by_sc, specs_by_sc = solve._extracted_inputs(tmp_path, tmp_path, index, ["S1"])
    assert facts_by_sc["S1"]["fx_rates"] == good_facts["fx_rates"]
    assert specs_by_sc["S1"]["clauses"] == {}
    assert specs_by_sc["S1"]["alarms"][0]["kind"] == "specs_failed"


def test_resolve_failure_keeps_spec_art(monkeypatch, tmp_path):
    """Транзиентный сбой resolve_doc_fact стоит максимум своего doc-ключа:
    уже извлечённые спеки заёмщика не заменяются пустышкой specs_failed
    (ревью PR #9, 26-я волна)."""
    import facts_extract as fe

    monkeypatch.setattr(solve, "find_inputs", lambda d: {"pdfs": []})
    monkeypatch.setattr(
        solve, "build_dossiers", lambda wd, pdfs, index, all_accounts=None: {"ACC-X": {"account_id": "ACC-X"}}
    )
    monkeypatch.setattr(solve, "extract_facts", lambda wd, d: fe._empty_facts())
    spec_art = {
        "clauses": {
            "6.1": {
                "valid": False,
                "errors": [],
                "missing_doc_keys": ["insurance_min"],
                "quote": "страховое покрытие",
                "direction": "min",
                "limit": "1",
                "trigger": None,
                "metric": "doc(insurance_min)",
                "template": None,
                "title_key": "",
            }
        },
        "alarms": [],
    }
    monkeypatch.setattr(solve, "extract_specs", lambda wd, d, keys: spec_art)

    def resolve_boom(*a, **k):
        raise RuntimeError("gemini 429 storm")

    monkeypatch.setattr(solve, "resolve_doc_fact", resolve_boom)
    index = {"scenario_to_account": {"S1": "ACC-X"}}
    _facts_by_sc, specs_by_sc = solve._extracted_inputs(tmp_path, tmp_path, index, ["S1"])
    assert "6.1" in specs_by_sc["S1"]["clauses"]  # спеки уцелели
    assert not any(a.get("kind") == "specs_failed" for a in specs_by_sc["S1"]["alarms"])
