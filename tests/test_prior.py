"""Приор — иерархия: пункт (точный) → семья → глобальный (36% точности)."""

import json

from prior import build_prior


def test_global_counts_match_spec():
    p = build_prior()
    assert p["global"] == {"BREACH": 17, "COMPLIANT": 19}


def test_conditional_keys_cover_all_cells():
    p = build_prior()
    assert sum(sum(v.values()) for v in p["by"].values()) == 36


def test_by_clause_covers_all_cells():
    p = build_prior()
    assert sum(sum(v.values()) for v in p["by_clause"].values()) == 36


def test_by_clause_has_pervasive_skew():
    p = build_prior()
    # 6.1 должна быть сильно в BREACH
    assert p["by_clause"]["6.1"].get("BREACH", 0) > p["by_clause"]["6.1"].get("COMPLIANT", 0)
    # 6.2 должна быть сильно в COMPLIANT
    assert p["by_clause"]["6.2"].get("COMPLIANT", 0) > p["by_clause"]["6.2"].get("BREACH", 0)


def test_written_json_matches(tmp_path):
    import prior

    out = tmp_path / "prior.json"
    prior.main(out)
    assert json.loads(out.read_text()) == build_prior()
