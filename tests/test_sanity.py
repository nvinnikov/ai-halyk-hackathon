"""До запуска на новом архиве: всё, что «не как в публичном», — список поломок."""

from pathlib import Path

from sanity import collect, diff_baselines

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


def test_diff_empty_on_identical():
    s = collect(PUBLIC_ZIP)
    assert diff_baselines(s, s) == []


def test_diff_catches_background_shift():
    s = collect(PUBLIC_ZIP)
    other = {**s, "background": {**s["background"], "rows": 8000}}
    d = diff_baselines(other, s)
    assert any("background" in line for line in d)
