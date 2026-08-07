"""Дешёвые детерминированные проверки, ловящие почти все катастрофы."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from invariants import (
    _collect_report_alarms,
    check_actuals_finite,
    check_background_share,
    check_breach_evidence,
    check_dirty_rows_recovered,
    check_dossier_binding,
    check_evidence_provenance,
    check_fallback_rate,
    check_fx_alarms,
    check_index_unique,
    check_other_share,
    check_reclass_applied,
    check_single_agreement,
    check_sum_conservation,
    check_template_keys,
    run_invariants,
)

import solve

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")
TEMPLATE = json.loads(Path("dataset/agentic-bank-public/submission_template.json").read_text())


def row(txn, cat, amt, cp="X"):
    return {
        "txn_id": txn,
        "cat": cat,
        "amt": Decimal(amt),
        "counterparty": cp,
        "description": "d",
        "date": "2025-06-01",
        "account_id": "ACC-1",
        "currency": "USD",
    }


def test_reclass_applied_catches_name_mismatch():
    raw = [row("T-1", "TAX", "-1", cp="Совсем Другое Имя")]
    facts = {"reclass": [{"txn": None, "counterparty": "Ertis Capital, LLP", "to": "INTEREST"}]}
    fails = check_reclass_applied("S1", raw, facts)
    assert fails and fails[0]["check"] == "reclass_applied"
    # а с совпадающим (по токенам) контрагентом — чисто
    ok = check_reclass_applied("S1", [row("T-1", "TAX", "-1", cp="Ertis Capital LLP")], facts)
    assert ok == []


def test_sum_conservation_clean_on_plain_rows():
    raw = [row("T-1", "TAX", "-1"), row("T-2", "OTHER", "-2")]
    assert check_sum_conservation("S1", raw, {}) == []


def test_sum_conservation_reconciles_exclude_and_override():
    raw = [row("T-1", "TAX", "-1"), row("T-2", "OTHER", "-2")]
    raw_dirty = raw + [{**row("T-3", "OTHER", "0"), "amt": None}]
    facts = {"exclude": ["T-1"], "amount_override": {"T-3": "-5"}}
    # T-1 отсечён документом (учтён в ожидании как отсечённый), T-3 — грязная
    # строка, восстановленная запиской казначейства: обе стороны сходятся.
    assert check_sum_conservation("S1", raw_dirty, facts) == []


def test_sum_conservation_catches_lost_row(monkeypatch):
    """prepare_rows, потерявший строку без документального решения, —
    ровно та потеря строк, которую этот инвариант обязан заметить."""
    import invariants

    raw = [row("T-1", "TAX", "-1"), row("T-2", "OTHER", "-2")]
    monkeypatch.setattr(invariants, "prepare_rows", lambda raw_rows, facts, overrides=None: raw_rows[:1])
    fails = check_sum_conservation("S1", raw, {})
    assert fails and fails[0]["check"] == "sum_conservation"


def test_other_share_critical():
    rows = [row("T-1", "OTHER", "-96"), row("T-2", "TAX", "-4")]
    assert check_other_share("S1", rows, {"CAPEX"}) == []  # OTHER не среди referenced
    fails = check_other_share("S1", rows, {"OTHER"})
    assert fails and fails[0]["check"] == "other_share_critical"


def test_dirty_rows_recovered():
    dirty = [{"txn_id": "T-9", "account_id": "ACC-1"}]
    assert check_dirty_rows_recovered("S1", dirty, {}) and (
        check_dirty_rows_recovered("S1", dirty, {})[0]["check"] == "dirty_row_unrecovered"
    )
    assert check_dirty_rows_recovered("S1", dirty, {"amount_override": {"T-9": "-1"}}) == []


def test_actuals_finite():
    answers = {"S1": {"6.1": {"status": "BREACH", "actual": 1.0, "evidence_txn_id": None}}}
    assert check_actuals_finite(answers) == []
    answers["S1"]["6.1"]["actual"] = float("nan")
    assert check_actuals_finite(answers)


def test_evidence_provenance():
    answers = {"S1": {"6.1": {"status": "BREACH", "actual": 1.0, "evidence_txn_id": "T-9"}}}
    traces = {("S1", "6.1"): {"evidence": [{"txn": "T-9", "decision_type": "reclass", "flipped": True}]}}
    assert check_evidence_provenance(answers, traces) == []
    traces[("S1", "6.1")]["evidence"] = []  # улика без кандидата из D
    assert check_evidence_provenance(answers, traces)


def test_breach_evidence_missing():
    answers = {"S1": {"6.1": {"status": "BREACH", "actual": 1.0, "evidence_txn_id": None}}}
    traces = {("S1", "6.1"): {"evidence": [{"txn": "T-9", "decision_type": "reclass", "flipped": True}]}}
    fails = check_breach_evidence(answers, traces)
    assert fails and fails[0]["check"] == "breach_evidence_missing"
    answers["S1"]["6.1"]["evidence_txn_id"] = "T-9"
    assert check_breach_evidence(answers, traces) == []


def test_template_keys():
    tpl = {"S1": {"6.1": {}}}
    assert check_template_keys({"S1": {"6.1": {}}}, tpl) == []
    assert check_template_keys({"S1": {"6.2": {}}}, tpl)
    assert check_template_keys({}, tpl)


def test_index_unique():
    assert check_index_unique({"alarms": []}) == []
    fails = check_index_unique({"alarms": [{"kind": "index_cardinality", "scenario": "S1"}]})
    assert fails and fails[0]["check"] == "index"


def test_background_share_bounds():
    assert check_background_share({"background": {"row_share": 0.5}}) == []
    assert check_background_share({"background": {"row_share": 0.01}})
    assert check_background_share({"background": {"row_share": 0.99}})


def test_fx_alarms_filters_by_kind_prefix():
    alarms = [{"kind": "fx_uncovered_row"}, {"kind": "fx_donor_used"}]
    fails = check_fx_alarms(alarms)
    assert len(fails) == 1 and fails[0]["alarm"]["kind"] == "fx_uncovered_row"


def test_single_agreement_zero_and_multiple():
    docs = [
        {"account_id": "ACC-1", "doc_type": "agreement", "quarantined": False, "edition": "final"},
        {"account_id": "ACC-2", "doc_type": "agreement", "quarantined": False, "edition": "final"},
        {"account_id": "ACC-2", "doc_type": "agreement", "quarantined": False, "edition": "final"},
        {"account_id": "ACC-3", "doc_type": "kyc", "quarantined": False, "edition": "final"},
    ]
    fails = check_single_agreement(docs, ["ACC-1", "ACC-2", "ACC-3"])
    by_acc = {f["account"]: f["n"] for f in fails}
    assert by_acc == {"ACC-2": 2, "ACC-3": 0}


def test_single_agreement_ignores_superseded_edition():
    """Перевыпущенный договор (старая редакция помечена superseded) — штатный
    случай, а не расхождение: у счёта остаётся ровно одна действующая."""
    docs = [
        {"account_id": "ACC-1", "doc_type": "agreement", "quarantined": False, "edition": "superseded"},
        {"account_id": "ACC-1", "doc_type": "agreement", "quarantined": False, "edition": "final"},
    ]
    assert check_single_agreement(docs, ["ACC-1"]) == []


def test_dossier_binding_requires_agreement_and_name_match():
    no_agreement = [{"account_id": "ACC-1", "docs": [{"doc_type": "kyc", "text": "x" * 10}]}]
    fails = check_dossier_binding(no_agreement)
    assert fails and fails[0]["check"] == "dossier_binding"

    mismatched = [
        {
            "account_id": "ACC-2",
            "docs": [
                {"doc_type": "agreement", "text": "Ekibastuz Energy JSC заёмщик по договору"},
                {"doc_type": "kyc", "text": "Совершенно другое юридическое лицо клиента банка"},
            ],
        }
    ]
    fails = check_dossier_binding(mismatched)
    assert fails and fails[0]["check"] == "dossier_binding_mismatch"

    matched = [
        {
            "account_id": "ACC-3",
            "docs": [
                {"doc_type": "agreement", "text": "Ekibastuz Energy JSC заёмщик по договору"},
                {"doc_type": "kyc", "text": "Клиент банка: Ekibastuz Energy JSC, реквизиты приложены"},
            ],
        }
    ]
    assert check_dossier_binding(matched) == []

    # KYC недоступен в досье — сверять не с чем, это не провал.
    no_kyc = [
        {"account_id": "ACC-4", "docs": [{"doc_type": "agreement", "text": "Договор ACC-4"}]},
    ]
    assert check_dossier_binding(no_kyc) == []


def test_fallback_rate_skips_without_baseline(tmp_path):
    traces = {("S1", "6.1"): {"tier": 2}}
    assert check_fallback_rate(traces, tmp_path / "missing.json") == []
    assert check_fallback_rate(traces, None) == []


def test_fallback_rate_against_baseline(tmp_path):
    baseline_path = tmp_path / "public_baseline.json"
    baseline_path.write_text(json.dumps({"fallback_rate": 0.5}))

    all_tier2 = {(f"S{i}", "6.1"): {"tier": 2} for i in range(36)}
    fails = check_fallback_rate(all_tier2, baseline_path)
    assert fails and fails[0]["check"] == "fallback_rate"

    all_tier0 = {(f"S{i}", "6.1"): {"tier": 0} for i in range(36)}
    assert check_fallback_rate(all_tier0, baseline_path) == []


def test_fallback_rate_baseline_alarm_printed_when_missing(tmp_path, capsys):
    from invariants import _print_fallback_rate_status

    _print_fallback_rate_status(tmp_path / "public_baseline.json")
    out = capsys.readouterr().out
    assert "ALARM fallback_rate_baseline_missing" in out


def test_fallback_rate_baseline_alarm_silent_when_present(tmp_path, capsys):
    from invariants import _print_fallback_rate_status

    baseline_path = tmp_path / "public_baseline.json"
    baseline_path.write_text(json.dumps({"fallback_rate": 0.5}))
    _print_fallback_rate_status(baseline_path)
    assert capsys.readouterr().out == ""


# --- синтетическая проверка сборки route/dossier-путей в run_invariants -----


def test_run_invariants_wires_route_and_dossier(tmp_path, monkeypatch):
    """check_single_agreement/check_dossier_binding в реальном прогоне
    покрыты только артефактами, оставшимися от предыдущего extracted-
    прогона workdir (случайность окружения, не гарантия). Здесь их
    подключение в run_invariants закреплено синтетикой: fake index.json +
    route/ + dossier/ в tmp_path, solve.extract_archive/load_ledger/
    scenario_inputs замоканы — реальный архив/датасет не нужен."""
    (tmp_path / "index.json").write_text(
        json.dumps(
            {
                "alarms": [],
                "background": {"row_share": 0.5},
                "scenario_to_account": {"S1": "ACC-1"},
                "account_to_scenario": {"ACC-1": "S1"},
            }
        )
    )
    (tmp_path / "trace").mkdir()

    route_dir = tmp_path / "route"
    route_dir.mkdir()
    for name in ("d1", "d2"):
        (route_dir / f"{name}.json").write_text(
            json.dumps(
                {"account_id": "ACC-1", "doc_type": "agreement", "quarantined": False, "edition": "final"}
            )
        )  # две одновременно "живые" редакции — неотфильтрованная (n=2)

    dossier_dir = tmp_path / "dossier"
    dossier_dir.mkdir()
    (dossier_dir / "ACC-1.json").write_text(
        json.dumps({"account_id": "ACC-1", "docs": [{"doc_type": "kyc", "text": "клиент без договора"}]})
    )  # в досье нет agreement

    monkeypatch.setattr(solve, "extract_archive", lambda archive: ("hash", tmp_path))
    monkeypatch.setattr(solve, "load_ledger", lambda wd, input_dir, target_scenarios=None: {"dirty": []})
    monkeypatch.setattr(solve, "scenario_inputs", lambda archive, sc: ([], {}))

    answers = {"S1": {"6.1": {"status": "COMPLIANT", "actual": 1.0, "evidence_txn_id": None}}}
    template_answers = {"S1": {"6.1": {}}}

    fails = run_invariants(Path("fake.zip"), tmp_path, answers, template_answers)
    checks = {f["check"] for f in fails}
    assert "single_agreement" in checks
    assert "dossier_binding" in checks


# --- интеграция: run_invariants на реальном expected-прогоне ----------------


@pytest.fixture(scope="module")
def public_run():
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    ds_hash, _ = solve.extract_archive(PUBLIC_ZIP)
    wd = solve.workdir(ds_hash)
    return answers, wd


def test_run_invariants_clean_on_public_expected(public_run):
    answers, wd = public_run
    fails = run_invariants(PUBLIC_ZIP, wd, answers, TEMPLATE["answers"])
    assert fails == [], fails


# --- _collect_report_alarms: видимость отравленных facts/specs (задача 31) --


def test_collect_report_alarms_sees_facts_and_specs_stage_failures(tmp_path):
    """facts_extraction_failed/specs_extraction_failed запекаются ВНУТРЬ
    build()-результата stages.artifact и кэшируются под текущей версией
    стадии (recovery-playbook.md) — отчёт обязан их видеть, иначе отравленный
    work/<hash> выглядит чистым."""
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts" / "ACC-1.json").write_text(
        json.dumps({"alarms": [{"kind": "facts_extraction_failed", "file": "a.pdf"}]})
    )
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "ACC-1.json").write_text(
        json.dumps({"alarms": [{"kind": "specs_extraction_failed", "error": "..."}]})
    )
    kinds = {a["kind"] for a in _collect_report_alarms(tmp_path)}
    assert "facts_extraction_failed" in kinds
    assert "specs_extraction_failed" in kinds
