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
from mutations import (
    main as mutations_main,
)

import solve
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


def _public_wd() -> Path:
    from ledger import extract_archive

    ds_hash, _ = extract_archive(PUBLIC_ZIP)
    return Path("work") / ds_hash


# Прогрев у мутаций двухуровневый, и мерить его одним признаком нельзя.
#
# text/ строится постранично через pypdf, без единого вызова LLM: его
# создаёт в том числе sanity (extract_pages), поэтому он есть
# после любого локального прогона и отсутствует в холодном CI. route/ —
# продукт документного конвейера, то есть требует ключа.
#
# build_renamed читает только text/vision (_preseed_text_vision), а
# shift_threshold и build_fx ходят в route/ (_final_agreement_doc,
# _treasury_accounts). Единый признак ошибается в обе стороны: по text/
# просыпаются route-тесты и падают на пустом route/ («ожидался ровно один
# действующий договор ..., найдено 0») — это ловилось на каждом втором
# локальном прогоне; по route/ глохнут rename-тесты, которые как раз могли
# бы идти без ключа. dossier/ здесь ни при чём — eval/mutations.py его не
# читает вовсе.


@pytest.fixture(scope="module")
def public_hash():
    """Прогрев уровня text/: достаточно для build_renamed."""
    wd = _public_wd()
    if not (wd / "text").is_dir():
        pytest.skip("публичный workdir не прогрет: нужен text/")
    return wd.name


@pytest.fixture(scope="module")
def public_hash_routed(public_hash):
    """Прогрев уровня route/: нужен там, где читается маршрутизация."""
    if not (_public_wd() / "route").is_dir():
        pytest.skip("публичный workdir не прогрет: нужен route/")
    return public_hash


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


def test_build_renamed_is_byte_deterministic_across_reruns(public_hash):
    """dataset_hash обязан зависеть только от содержимого, не от времени сборки
    zip: ZipInfo без явного date_time штампует datetime.now(), и повторный
    прогон той же мутации будил бы routing/facts/specs заново вместо
    переиспользования уже посчитанного workdir (регрессия, найденная на
    живом прогоне shift 2026-08-07 — см. docs/ops/task-28-report.md)."""
    h1 = dataset_hash(build_renamed(PUBLIC_ZIP))
    h2 = dataset_hash(build_renamed(PUBLIC_ZIP))
    assert h1 == h2


def test_shift_threshold_is_byte_deterministic_across_reruns(public_hash_routed):
    h1 = dataset_hash(shift_threshold(PUBLIC_ZIP, "B1", "6.1"))
    h2 = dataset_hash(shift_threshold(PUBLIC_ZIP, "B1", "6.1"))
    assert h1 == h2


def test_build_fx_is_byte_deterministic_across_reruns(public_hash_routed):
    h1 = dataset_hash(build_fx(PUBLIC_ZIP, n_rows=5))
    h2 = dataset_hash(build_fx(PUBLIC_ZIP, n_rows=5))
    assert h1 == h2


def test_shift_threshold_produces_new_key_with_shifted_value(public_hash_routed):
    scenario, clause = "B1", "6.1"
    old = float(SPECS[scenario][clause][2])
    new = round(old * 0.72, 2)

    out_zip = shift_threshold(PUBLIC_ZIP, scenario, clause)
    mut_hash = dataset_hash(out_zip)
    assert mut_hash != public_hash_routed

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


def test_shift_threshold_guard_on_noop(public_hash_routed, monkeypatch):
    import expected_extraction

    # порог, которого нет в тексте договора ни в каком разумном формате
    broken = {**expected_extraction.SPECS["B1"], "6.1": ("icr", "min", 999999.87)}
    monkeypatch.setitem(expected_extraction.SPECS, "B1", broken)
    with pytest.raises(RuntimeError, match="no-op"):
        shift_threshold(PUBLIC_ZIP, "B1", "6.1")


def test_build_fx_converts_rows_and_preseeds_rate_line(public_hash_routed):
    out_zip = build_fx(PUBLIC_ZIP, n_rows=5)
    mut_hash = dataset_hash(out_zip)
    assert mut_hash != public_hash_routed

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


def test_build_fx_guard_on_no_eligible_rows(public_hash_routed, monkeypatch):
    import mutations

    monkeypatch.setattr(mutations, "_treasury_accounts", lambda pub_wd, s2a: {})
    with pytest.raises(RuntimeError):
        build_fx(PUBLIC_ZIP)


def _snapshot_out(out_dir: Path) -> dict[str, bytes]:
    if not out_dir.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in sorted(out_dir.glob("*.json"))}


def test_mutations_main_does_not_touch_real_out(tmp_path, monkeypatch):
    """mutations.main зовёт solve.main дважды подряд (baseline + мутация) —
    без изоляции solve.OUT второй вызов писал бы submission поверх боевого
    out/ (см. tests/test_faults.py, тот же приём снапшота)."""
    import mutations

    archive = tmp_path / "fake.zip"
    archive.write_bytes(b"fake archive bytes for hashing")
    monkeypatch.setattr(mutations, "build_renamed", lambda archive: archive)

    seen_out: list[Path] = []

    def fake_solve_main(archive_, **kw):
        seen_out.append(solve.OUT)
        solve.OUT.mkdir(parents=True, exist_ok=True)
        (solve.OUT / "submission.json").write_text("{}")
        return {}

    monkeypatch.setattr(solve, "main", fake_solve_main)

    real_out = Path("out")
    before = _snapshot_out(real_out)

    ok = mutations_main(archive, "rename")

    assert ok is True
    assert len(seen_out) == 2
    real_out_resolved = real_out.resolve()
    assert all(p.resolve() != real_out_resolved for p in seen_out), (
        "solve.OUT указывал на боевой out/ хотя бы в одном из вызовов"
    )
    assert _snapshot_out(real_out) == before, "боевой out/ изменился — изоляция solve.OUT не сработала"
