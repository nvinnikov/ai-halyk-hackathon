"""Приор условен по (направление, семья метрики); безопасного дефолта нет: 17/19."""

import json

from prior import build_prior


def test_global_counts_match_spec():
    p = build_prior()
    assert p["global"] == {"BREACH": 17, "COMPLIANT": 19}


def test_conditional_keys_cover_all_cells():
    p = build_prior()
    assert sum(sum(v.values()) for v in p["by"].values()) == 36


def test_written_json_matches(tmp_path):
    import prior

    out = tmp_path / "prior.json"
    prior.main(out)
    assert json.loads(out.read_text()) == build_prior()
