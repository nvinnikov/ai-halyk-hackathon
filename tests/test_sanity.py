"""До запуска на новом архиве: всё, что «не как в публичном», — список поломок."""

import json
from pathlib import Path

from sanity import _resolve_fallback_rate, _resolve_public_score, _stage_alarms, collect, diff_baselines

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")


def test_collect_matches_spec_numbers():
    s = collect(PUBLIC_ZIP)
    assert s["targets"] == 12
    assert s["background"]["accounts"] == 549
    assert s["background"]["rows"] == 800
    assert s["currencies_target"] == {"EUR": 15}
    assert s["clauses"] == ["6.1", "6.2", "6.3"]
    assert s["pdf_count"] > 10
    # Правка брифа ждала 9 (задача 18, замер до фикса футера страницы,
    # commit 666c6a6). Прямой замер (200 PDF, is_blind по правилу «И»)
    # даёт 8 и на текущем, и на дофиксовом pdftext.py — расхождение не в
    # детекторе, а в описке при переносе числа в план.
    assert s["blind_pages"] == 8
    # Пограничные (ровно один критерий слепоты из двух) на публичном наборе —
    # 160 из 843 страниц (~19%). Само число — калибровка, не инвариант;
    # смысл счётчика в дифе против baseline: резкий рост на приватном
    # наборе = сканов больше, правило «И» пора переключать на «ИЛИ»
    # (research §3, оговорка).
    assert s["borderline_pages"] == 160


def test_diff_empty_on_identical():
    s = collect(PUBLIC_ZIP)
    assert diff_baselines(s, s) == []


def test_diff_catches_background_shift():
    s = collect(PUBLIC_ZIP)
    other = {**s, "background": {**s["background"], "rows": 8000}}
    d = diff_baselines(other, s)
    assert any("background" in line for line in d)


def test_resolve_fallback_rate_keeps_old_number_on_new_none():
    # None поверх числа — регресс данных (например, трейсы затёрлись
    # expected-режимом), не первая генерация: старое значение не затирается.
    rate, warning = _resolve_fallback_rate(None, {"fallback_rate": 0.25})
    assert rate == 0.25
    assert warning is not None


def test_resolve_fallback_rate_writes_new_number_over_none():
    rate, warning = _resolve_fallback_rate(0.5, {"fallback_rate": None})
    assert rate == 0.5
    assert warning is None


def test_resolve_fallback_rate_writes_new_number_first_generation():
    # Baseline ещё не существует (первая генерация) — писать без предупреждений.
    rate, warning = _resolve_fallback_rate(0.5, None)
    assert rate == 0.5
    assert warning is None


def test_resolve_fallback_rate_none_over_none_no_warning():
    rate, warning = _resolve_fallback_rate(None, {"fallback_rate": None})
    assert rate is None
    assert warning is None


# --- public_score: якорь скоринга публичного набора, не поле collect() -----
# (ревью PR #12: diff_baselines шумел DIFF public_score: N -> None на каждом
# запуске, --write-baseline тихо стирал якорь при перегенерации baseline) --


def test_diff_baselines_silent_on_public_score():
    # collect() никогда не отдаёт public_score — без skip это вечный DIFF.
    s = collect(PUBLIC_ZIP)
    base = {**s, "public_score": 12.34}
    assert diff_baselines(s, base) == []


def test_resolve_public_score_keeps_old_value_on_write_baseline():
    # --write-baseline не должен молча стирать ранее записанный якорь.
    assert _resolve_public_score({"public_score": 12.34}) == 12.34


def test_resolve_public_score_none_on_first_generation():
    assert _resolve_public_score(None) is None


# --- stage_alarms: видимость отравленных facts/specs/route/dossier ---------
# (docs/ops/recovery-playbook.md, задача 31) ------------------------------------------


def test_stage_alarms_none_before_any_extracted_run(tmp_path):
    assert _stage_alarms(tmp_path) is None


def test_stage_alarms_counts_kinds_across_all_four_dirs(tmp_path):
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts" / "ACC-1.json").write_text(
        json.dumps({"alarms": [{"kind": "facts_extraction_failed", "file": "a.pdf"}]})
    )
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "ACC-1.json").write_text(
        json.dumps({"alarms": [{"kind": "no_agreement", "account": "ACC-1"}]})
    )
    (tmp_path / "route").mkdir()
    (tmp_path / "route" / "h1.json").write_text(
        json.dumps({"alarms": [{"kind": "meta_extraction_failed", "file": "a.pdf"}]})
    )
    (tmp_path / "dossier").mkdir()
    (tmp_path / "dossier" / "ACC-1.json").write_text(
        json.dumps({"alarms": [{"kind": "dossier_build_failed", "account": "ACC-1"}]})
    )
    assert _stage_alarms(tmp_path) == {
        "facts_extraction_failed": 1,
        "no_agreement": 1,
        "meta_extraction_failed": 1,
        "dossier_build_failed": 1,
    }


def test_stage_alarms_empty_dict_when_dirs_exist_but_clean(tmp_path):
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts" / "ACC-1.json").write_text(json.dumps({"alarms": []}))
    assert _stage_alarms(tmp_path) == {}
