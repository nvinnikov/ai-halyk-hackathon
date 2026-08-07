"""Новый ключ мутации выводится БЕЗ нашего движка — иначе тест проверяет
самосогласованность, а не правильность.

Юниты ниже — чисто файловые (build_renamed/shift_threshold/build_fx читают уже
закэшированные text/vision-артефакты публичного workdir и не ходят в LLM).
Сквозная проверка через solve.main — в test_mutations_llm.py, маркер llm.
"""

import json
from pathlib import Path

import pytest
from expected_extraction import SPECS
from mutations import (
    apply_renames,
    build_fx,
    build_renamed,
    predict_status,
    rename_map,
    shift_threshold,
)

from util import dataset_hash, workdir

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")


def test_rename_map_covers_all_names():
    from expected_extraction import FACTS

    names = {n for f in FACTS.values() for n in f.get("related_parties", [])}
    names |= {n for f in FACTS.values() for n in f.get("unrestricted_subsidiaries", [])}
    m = rename_map()
    for name in names:
        first_token = [w for w in name.split() if len(w) > 3][0]
        assert first_token in m, f"нет замены для {name}"
        assert m[first_token] != first_token


def test_apply_renames_keeps_numbers():
    m = {"Ertis": "Almaz"}
    text = "Платёж Ertis Capital LLP на 486,204.19 от Ertis."
    out = apply_renames(text, m)
    assert "Ertis" not in out and "Almaz" in out
    assert "486,204.19" in out


def test_apply_renames_word_boundaries():
    assert apply_renames("Ertisov Ertis", {"Ertis": "Almaz"}) == "Ertisov Almaz"


def test_apply_renames_counts_hits():
    hits: dict[str, int] = {}
    apply_renames("Ertis платит Ertis снова, а Kazyna молчит.", {"Ertis": "Almaz", "Kazyna": "Orda"}, hits)
    assert hits == {"Ertis": 2, "Kazyna": 1}


def test_apply_renames_no_hit_not_counted():
    hits: dict[str, int] = {}
    apply_renames("здесь никого нет", {"Ertis": "Almaz"}, hits)
    assert hits.get("Ertis", 0) == 0


def test_predict_status_without_engine():
    # старый actual из ключа против нового порога
    assert predict_status(9.45, "max", 6.50) == "BREACH"
    assert predict_status(9.45, "max", 10.00) == "COMPLIANT"
    assert predict_status(1.5, "min", 2.00) == "BREACH"


@pytest.fixture(scope="module")
def public_hash():
    from ledger import extract_archive

    ds_hash, _ = extract_archive(PUBLIC_ZIP)
    assert (Path("work") / ds_hash / "text").is_dir(), "публичный workdir не прогрет — нужен полный прогон"
    return ds_hash


def test_build_renamed_produces_valid_archive_with_preseeded_text(public_hash):
    out_zip = build_renamed(PUBLIC_ZIP)
    assert out_zip.exists()
    mut_hash = dataset_hash(out_zip)
    assert mut_hash != public_hash  # новый ключ — байты архива другие

    mut_wd = workdir(mut_hash)
    # хотя бы часть переименований действительно попала в предзасеянный текст
    m = rename_map()
    found = False
    for f in sorted((mut_wd / "text").glob("*.json")):
        art = json.loads(f.read_text())
        for page in art.get("pages", []):
            if any(new in page["text"] for new in m.values()):
                found = True
    assert found, "ни одна замена не попала в предзасеянный текст"


def test_build_renamed_guard_on_noop(public_hash, monkeypatch):
    import mutations

    # токен, которого заведомо нет ни в CSV, ни в тексте документов
    monkeypatch.setattr(mutations, "_RENAMES", {**mutations._RENAMES, "Ozxqvywk": "Ничто"})
    with pytest.raises(RuntimeError, match="no-op"):
        build_renamed(PUBLIC_ZIP)


def test_shift_threshold_produces_new_key_with_shifted_value(public_hash):
    scenario, clause = "B1", "6.1"
    old = float(SPECS[scenario][clause][2])
    new = round(old * 0.72, 2)

    out_zip = shift_threshold(PUBLIC_ZIP, scenario, clause)
    mut_hash = dataset_hash(out_zip)
    assert mut_hash != public_hash

    mut_wd = workdir(mut_hash)
    combined = ""
    for f in sorted((mut_wd / "text").glob("*.json")):
        art = json.loads(f.read_text())
        for page in art.get("pages", []):
            combined += page["text"]
    assert f"{new:.2f}" in combined
    # Старая форма порога («2.00x») не должна остаться в сегменте пункта 6.1;
    # голое число «2.00» не годится — оно совпадает с несвязанной неустойкой
    # («ставку на 2.00% годовых») в другой статье того же договора.
    assert f"{old:.2f}x" not in combined or old == new


def test_shift_threshold_guard_on_noop(public_hash, monkeypatch):
    import expected_extraction

    # порог, которого нет в тексте договора ни в каком разумном формате
    broken = {**expected_extraction.SPECS["B1"], "6.1": ("icr", "min", 999999.87)}
    monkeypatch.setitem(expected_extraction.SPECS, "B1", broken)
    with pytest.raises(RuntimeError, match="no-op"):
        shift_threshold(PUBLIC_ZIP, "B1", "6.1")


def test_build_fx_converts_rows_and_preseeds_rate_line(public_hash):
    out_zip = build_fx(PUBLIC_ZIP, n_rows=5)
    mut_hash = dataset_hash(out_zip)
    assert mut_hash != public_hash

    import zipfile

    with zipfile.ZipFile(out_zip) as z:
        csv_name = next(n for n in z.namelist() if n.endswith("master_ledger_2025.csv"))
        csv_text = z.read(csv_name).decode()
    assert "EUR" in csv_text

    mut_wd = workdir(mut_hash)
    combined = ""
    for f in sorted((mut_wd / "text").glob("*.json")):
        art = json.loads(f.read_text())
        for page in art.get("pages", []):
            combined += page["text"]
    assert "1.16" in combined


def test_build_fx_guard_on_no_eligible_rows(public_hash, monkeypatch):
    import mutations

    monkeypatch.setattr(mutations, "_treasury_accounts", lambda pub_wd, s2a: {})
    with pytest.raises(RuntimeError):
        build_fx(PUBLIC_ZIP)
