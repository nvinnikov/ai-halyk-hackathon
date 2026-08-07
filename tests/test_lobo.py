"""LOBO: скрытый заёмщик не пользуется библиотекой шаблонов — ловим подгонку."""

import json
from pathlib import Path

import lobo

import solve

SP = {"metric": "agg(CAPEX, net)", "template": "capex", "valid": True}


def test_hidden_scenario_uses_raw_metric():
    assert solve._metric_text_for(SP, "S1", frozenset({"S1"})) == "agg(CAPEX, net)"


def test_visible_scenario_uses_template():
    from templates import TEMPLATES

    assert solve._metric_text_for(SP, "S1", frozenset()) == TEMPLATES["capex"]


def test_no_template_match_always_raw():
    sp = {**SP, "template": None}
    assert solve._metric_text_for(sp, "S1", frozenset()) == "agg(CAPEX, net)"


def _snapshot_out(out_dir: Path) -> dict[str, bytes]:
    if not out_dir.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in sorted(out_dir.glob("*.json"))}


def test_lobo_main_does_not_touch_real_out(tmp_path, monkeypatch):
    """eval/lobo.py гоняет solve.main 13 раз (baseline + 12 сценариев) —
    без изоляции solve.OUT каждый из них писал бы submission поверх боевого
    out/ (см. tests/test_faults.py, тот же приём снапшота)."""
    archive = tmp_path / "fake.zip"
    archive.write_bytes(b"fake archive bytes for hashing")

    gt = json.loads(lobo.GT_PATH.read_text())["scenarios"]
    seen_out: list[Path] = []

    def fake_solve_main(archive_, **kw):
        seen_out.append(solve.OUT)
        solve.OUT.mkdir(parents=True, exist_ok=True)
        (solve.OUT / "submission.json").write_text("{}")
        return dict.fromkeys(gt, {})

    monkeypatch.setattr(solve, "main", fake_solve_main)

    real_out = Path("out")
    before = _snapshot_out(real_out)

    lobo.main(archive)

    assert len(seen_out) == 1 + len(gt)
    real_out_resolved = real_out.resolve()
    assert all(p.resolve() != real_out_resolved for p in seen_out), (
        "solve.OUT указывал на боевой out/ хотя бы в одном из вызовов"
    )
    assert _snapshot_out(real_out) == before, "боевой out/ изменился — изоляция solve.OUT не сработала"
