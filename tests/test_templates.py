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
from dsl import parse, signature
from interp import Ctx, evaluate
from legacy_metrics import M
from templates import TEMPLATE_HEADINGS, TEMPLATES, match_heading, match_signature, title_key
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


def test_nineteen_headings_map_to_nineteen_distinct_keys():
    # Основной путь матча (задача 24): заголовок пункта однозначно определяет
    # метрику — 19 заголовков на 19 метрик, распределение один-в-один.
    assert len(TEMPLATE_HEADINGS) == 19
    assert len(set(TEMPLATE_HEADINGS.values())) == 19


def test_match_heading_roundtrip():
    for key, name in TEMPLATE_HEADINGS.items():
        assert match_heading(key) == name


def test_match_heading_unknown_returns_none():
    assert match_heading(title_key("совершенно другой заголовок пункта")) is None


def _perturb(text: str) -> dict[str, str]:
    """Как приватный набор мог бы сформулировать тот же заголовок иначе."""
    words = text.split()
    return {
        "выброшено последнее слово": " ".join(words[:-1]),
        "добавлено слово": text + " Заёмщика",
        "переставлены два последних слова": " ".join(words[:-2] + words[-1:] + words[-2:-1])
        if len(words) > 2
        else text,
    }


def test_heading_similarity_separates_own_from_foreign():
    """Зазор, на котором стоят пороги нестрогого матча — здесь он и меряется.

    Числа в `solution/templates.py` (порог сходства и требуемый отрыв) держатся
    ровно на этом факте: переформулированный заголовок похож на СВОЙ шаблон
    заметно сильнее, чем любые два РАЗНЫХ шаблона похожи друг на друга. Если
    зазор схлопнется — тест упадёт раньше, чем нестрогий матч начнёт подбирать
    чужие формулы.
    """
    from templates import (
        _HEADING_TOKENS,
        _MIN_HEADING_MARGIN_PCT,
        _MIN_HEADING_SIMILARITY_PCT,
        _MIN_HEADING_TOKEN,
        _TEMPLATE_HEADING_TEXT,
        heading_similarity_pct,
    )

    def toks(text):
        return frozenset(w for w in title_key(text).split() if len(w) >= _MIN_HEADING_TOKEN)

    names = sorted(_HEADING_TOKENS)
    foreign_peak = max(
        heading_similarity_pct(_HEADING_TOKENS[a], _HEADING_TOKENS[b])
        for i, a in enumerate(names)
        for b in names[i + 1 :]
    )
    own_floor = min(
        heading_similarity_pct(toks(fn(h)), _HEADING_TOKENS[name])
        for name, h in _TEMPLATE_HEADING_TEXT.items()
        for fn in (lambda t: " ".join(t.split()[:-1]), lambda t: t + " Заёмщика")
    )
    assert foreign_peak == 40 and own_floor == 66, (foreign_peak, own_floor)
    assert foreign_peak < _MIN_HEADING_SIMILARITY_PCT <= own_floor
    assert _MIN_HEADING_MARGIN_PCT <= own_floor - foreign_peak


def test_match_heading_survives_reworded_title():
    """Матч заголовка деградирует, а не обрывается.

    До этого матч был точным поиском по словарю: одно слово иначе — и ни один
    из 19 заголовков не срабатывал, а вместе с ними уходило 5.00 балла
    (замер LOBO: 34.50 → 29.50). Формулировки приватных договоров нам
    неизвестны, поэтому близкий по словам заголовок обязан находить свой
    шаблон.
    """
    from templates import _TEMPLATE_HEADING_TEXT

    for name, heading in _TEMPLATE_HEADING_TEXT.items():
        for label, variant in _perturb(heading).items():
            assert match_heading(title_key(variant)) == name, f"{name}: {label}"


def test_match_heading_never_picks_a_foreign_template():
    """Неверный шаблон хуже отсутствия матча: формула подменится молча.

    Проверяем обе стороны: пертурбированный заголовок находит СВОЙ шаблон и
    ничей больше, а заголовок, собранный из слов двух разных шаблонов,
    отвергается как неоднозначный.
    """
    from templates import _TEMPLATE_HEADING_TEXT

    for name, heading in _TEMPLATE_HEADING_TEXT.items():
        for variant in _perturb(heading).values():
            got = match_heading(title_key(variant))
            assert got in (name, None), f"{name} сматчился на чужой шаблон {got}"

    names = sorted(_TEMPLATE_HEADING_TEXT)
    mixed = _TEMPLATE_HEADING_TEXT[names[0]] + " " + _TEMPLATE_HEADING_TEXT[names[1]]
    assert match_heading(title_key(mixed)) is None


def test_match_heading_unrelated_title_still_none():
    """Порог не должен превращать библиотеку в «что-нибудь подберём»."""
    assert match_heading(title_key("Порядок уведомления сторон о смене реквизитов")) is None
    assert match_heading(title_key("Ответственность за нарушение сроков поставки")) is None


def test_title_key_is_language_independent():
    # Английский заголовок при русском окружении матчится тем же ключом:
    # регистр, пунктуация и цифры значения не имеют.
    key_a = title_key("Maximum Capital Intensity Ratio")
    key_b = title_key("6.1. maximum CAPITAL intensity ratio!")
    assert key_a == key_b
    assert match_heading(key_a) == "capital_intensity"


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
