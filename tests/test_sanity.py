"""До запуска на новом архиве: всё, что «не как в публичном», — список поломок."""

import json
from pathlib import Path

import sanity
from ledger import extract_archive
from sanity import collect, diff_baselines
from util import workdir

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")


def test_collect_matches_spec_numbers():
    s = collect(PUBLIC_ZIP)
    assert s["targets"] == 12
    assert s["background"]["accounts"] == 549
    assert s["background"]["rows"] == 800
    assert s["currencies_target"] == {"EUR": 15}
    assert s["clauses"] == ["6.1", "6.2", "6.3"]
    assert s["pdf_count"] == 200
    # 8, а не 9 из правки плана: замерено 8 слепых страниц в 5 документах, и это
    # ровно то, что стоит в таблице research-дока (раздел 3) и совпадает с
    # перечнем из пяти документов в разделе 2. «9» в плане — описка; детектор
    # разобран (см. коммит), а не подогнан.
    assert s["blind_pages"] == 8
    assert s["dirty_rows"] == 2
    # Сигнал про сканы — только «мало символов, но числа есть»; обратное
    # направление (159) — титулы и оглавления, стабильный шум.
    assert s["blind_borderline"] == {"short_with_numbers": 1, "long_with_few_numbers": 159}


def test_diff_empty_on_identical():
    s = collect(PUBLIC_ZIP)
    assert diff_baselines(s, s) == []


def test_diff_catches_background_shift():
    s = collect(PUBLIC_ZIP)
    other = {**s, "background": {**s["background"], "rows": 8000}}
    d = diff_baselines(other, s)
    assert any("background" in line for line in d)


def test_diff_ignores_dataset_hash():
    """Хеш обязан отличаться между наборами — это не диф, а норма.
    Отдельное предупреждение печатает main(), если он, наоборот, совпал."""
    s = collect(PUBLIC_ZIP)
    other = {**s, "dataset_hash": "0" * 64}
    assert diff_baselines(other, s) == []


def test_collect_does_not_clobber_shared_ledger_artifact():
    """Sanity идёт первым в окне 9 августа (11:00–11:10) и обязан быть без LLM,
    то есть строит леджер без второго яруса категоризации. Если бы он писал общий
    work/<hash>/ledger.json, solve потом молча переиспользовал бы артефакт без
    LLM-категорий: расход остался бы в OTHER и завысил EBITDA. Отсюда отдельный
    рабочий каталог под леджер sanity."""
    ds_hash, _ = extract_archive(PUBLIC_ZIP)
    shared = workdir(ds_hash) / "ledger.json"
    before = shared.read_bytes() if shared.exists() else None
    collect(PUBLIC_ZIP)
    after = shared.read_bytes() if shared.exists() else None
    assert after == before


def test_doc_types_reported_without_run():
    """Спека требует «сколько документов и каких типов». Разбивка добирается из
    route-артефактов прошлого прогона; прогона не было — так и печатается,
    LLM из sanity не зовётся ни при каких условиях."""
    s = collect(PUBLIC_ZIP)
    assert isinstance(s["doc_types"], dict | str)
    if isinstance(s["doc_types"], str):
        assert s["doc_types"].startswith("unknown")


def test_fallback_rate_key_present():
    """Потолок для check_fallback_rate (задача 26) живёт в этом же снимке."""
    s = collect(PUBLIC_ZIP)
    assert "fallback_rate" in s
    assert s["fallback_rate"] is None or 0.0 <= s["fallback_rate"] <= 1.0


def test_fallback_rate_ignores_reference_run(tmp_path, monkeypatch):
    """Эталонный прогон фолбэков не даёт по построению, и записанный с него
    потолок 0.0 сделал бы инвариант 26 недостижимым. Учитывается только
    extracted-прогон."""
    trace = tmp_path / "trace"
    trace.mkdir()
    (trace / "S1.borrower.json").write_text(json.dumps({"facts_source": "expected"}))
    (trace / "S1.6.1.json").write_text(json.dumps({"tier": 0}))
    assert sanity._fallback_rate(tmp_path) is None

    (trace / "S1.borrower.json").write_text(json.dumps({"facts_source": "extracted"}))
    (trace / "S1.6.2.json").write_text(json.dumps({"tier": 2}))
    assert sanity._fallback_rate(tmp_path) == 0.5
