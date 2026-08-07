"""LOBO: скрытый заёмщик не пользуется библиотекой шаблонов — ловим подгонку."""

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
