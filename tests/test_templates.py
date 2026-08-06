"""Каждая из 19 метрик выражается в DSL и даёт тот же результат — приёмка DSL.

«Бит в бит» интерпретируется как: submission-значение (q2) совпадает и
относительное расхождение сырых значений < 1e-9 (старое ядро — float,
новое — Decimal, битовая идентичность между типами не определена).
"""

from decimal import Decimal
from pathlib import Path

import pytest
from expected_extraction import SPECS

import solve
from covenants import M
from dsl import parse, signature
from interp import Ctx, evaluate
from templates import TEMPLATES, match_signature
from util import q2

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")


def test_all_metrics_have_templates():
    used = {spec[0] for cells in SPECS.values() for spec in cells.values()}
    assert used <= set(TEMPLATES)


def test_templates_parse_and_have_unique_signatures():
    sigs = {}
    for name, text in sorted(TEMPLATES.items()):
        sig = signature(parse(text))
        assert sig not in sigs, f"{name} и {sigs[sig]} неразличимы по сигнатуре"
        sigs[sig] = name


def test_match_signature_roundtrip():
    for name, text in TEMPLATES.items():
        assert match_signature(parse(text)) == name


CELLS = [(sc, cl) for sc in sorted(SPECS) for cl in sorted(SPECS[sc])]


@pytest.mark.parametrize(("sc", "cl"), CELLS, ids=[f"{s}-{c}" for s, c in CELLS])
def test_dsl_parity_with_legacy_metric(sc, cl):
    from engine import prepare_rows

    raw, facts = solve.scenario_inputs(PUBLIC_ZIP, sc)
    assert "doc_facts" in facts  # scenario_inputs обязан пропустить факты через адаптер
    rows = prepare_rows(raw, facts)  # легаси-метрики ждут строки после фактов
    name = SPECS[sc][cl][0]
    legacy = Decimal(str(M[name](rows, facts)))
    got = evaluate(parse(TEMPLATES[name]), Ctx(rows=rows, facts=facts)).value
    assert q2(abs(got)) == q2(abs(legacy))
    if legacy:
        assert abs((got - legacy) / legacy) < Decimal("1e-9")
