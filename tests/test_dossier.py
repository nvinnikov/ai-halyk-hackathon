"""Действующая редакция: последняя по дате, маркер перебивает дату."""

from pathlib import Path

import dossier

INDEX = {"scenario_to_account": {"S1": "ACC-1"}, "account_to_scenario": {"ACC-1": "S1"}}


def make_route(monkeypatch, routes, texts):
    monkeypatch.setattr(dossier, "route_doc", lambda wd, p, targets, all_accounts: routes[p.name])
    monkeypatch.setattr(dossier, "full_text", lambda wd, p: texts.get(p.name, ""))
    # Файлы в тестах синтетические: hash — это имя, коллизий нет.
    monkeypatch.setattr(dossier, "doc_hash", lambda p: f"hash-{p.name}")


def base(file, dtype="agreement", date="2025-01-01", edition="unmarked", acc="ACC-1", reason=None):
    return {
        "file": file,
        "doc_hash": f"hash-{file}",
        "account_id": acc,
        "doc_type": dtype,
        "date": date,
        "edition": edition,
        "mentions": [acc] if acc else [],
        "mentions_nontarget": [],
        "quarantined": acc is None,
        "quarantine_reason": reason,
        "alarms": [],
        "routing_quote": "",
    }


def test_later_date_wins(monkeypatch, tmp_path):
    routes = {"a.pdf": base("a.pdf", date="2025-01-01"), "b.pdf": base("b.pdf", date="2025-06-01")}
    make_route(monkeypatch, routes, {"a.pdf": "old", "b.pdf": "new"})
    d = dossier.build_dossiers(tmp_path, [Path("a.pdf"), Path("b.pdf")], INDEX)["ACC-1"]
    assert [x["file"] for x in d["docs"]] == ["b.pdf"]
    assert d["docs_rejected"][0]["file"] == "a.pdf"
    assert d["docs_rejected"][0]["reason"] == "superseded_by_date"
    assert d["docs_rejected"][0]["kept"] == "b.pdf"


def test_final_marker_beats_date(monkeypatch, tmp_path):
    routes = {
        "a.pdf": base("a.pdf", date="2025-01-01", edition="final"),
        "b.pdf": base("b.pdf", date="2025-06-01", edition="draft"),
    }
    make_route(monkeypatch, routes, {})
    d = dossier.build_dossiers(tmp_path, [Path("a.pdf"), Path("b.pdf")], INDEX)["ACC-1"]
    assert [x["file"] for x in d["docs"]] == ["a.pdf"]
    assert d["docs_rejected"][0]["reason"] == "edition_marker"


def test_different_types_both_kept(monkeypatch, tmp_path):
    routes = {"a.pdf": base("a.pdf", dtype="agreement"), "b.pdf": base("b.pdf", dtype="kyc")}
    make_route(monkeypatch, routes, {})
    d = dossier.build_dossiers(tmp_path, [Path("a.pdf"), Path("b.pdf")], INDEX)["ACC-1"]
    assert len(d["docs"]) == 2


def test_quarantined_listed_with_reason(monkeypatch, tmp_path):
    routes = {
        "a.pdf": base("a.pdf"),
        "q.pdf": base("q.pdf", acc=None, reason="background_document"),
    }
    make_route(monkeypatch, routes, {})
    d = dossier.build_dossiers(tmp_path, [Path("a.pdf"), Path("q.pdf")], INDEX)["ACC-1"]
    assert d["quarantined"] == [{"file": "q.pdf", "reason": "background_document"}]


def test_same_basename_in_different_dirs(monkeypatch, tmp_path):
    """Коллизия базовых имён во вложенных каталогах не роняет сшивку:
    документы адресуются doc_hash, а не именем."""
    routes = {"x.pdf": base("x.pdf", date="2025-06-01")}
    monkeypatch.setattr(dossier, "route_doc", lambda wd, p, targets, all_accounts: routes["x.pdf"])
    monkeypatch.setattr(dossier, "full_text", lambda wd, p: str(p))
    monkeypatch.setattr(dossier, "doc_hash", lambda p: "hash-x.pdf" if "sub" in str(p) else "other")
    pdfs = [Path("x.pdf"), Path("sub/x.pdf")]
    d = dossier.build_dossiers(tmp_path, pdfs, INDEX)["ACC-1"]
    # текст берётся у файла с совпавшим doc_hash — sub/x.pdf
    assert len(d["docs"]) == 1 and "sub" in d["docs"][0]["text"]


def test_cumulative_types_keep_all_docs(monkeypatch, tmp_path):
    """Записки казначейства кумулятивны: каждая несёт своё решение, отброс
    по дате терял бы факты."""
    routes = {
        "m1.pdf": base("m1.pdf", dtype="treasury_memo", date="2025-01-01"),
        "m2.pdf": base("m2.pdf", dtype="treasury_memo", date="2025-06-01"),
    }
    make_route(monkeypatch, routes, {})
    d = dossier.build_dossiers(tmp_path, [Path("m1.pdf"), Path("m2.pdf")], INDEX)["ACC-1"]
    assert [x["file"] for x in d["docs"]] == ["m1.pdf", "m2.pdf"]
    assert d["docs_rejected"] == []


def test_single_superseded_is_never_active(monkeypatch, tmp_path):
    routes = {"a.pdf": base("a.pdf", edition="superseded")}
    make_route(monkeypatch, routes, {})
    d = dossier.build_dossiers(tmp_path, [Path("a.pdf")], INDEX)["ACC-1"]
    assert d["docs"] == []
    assert d["docs_rejected"][0]["reason"] == "superseded_edition"


def test_routing_failed_dossier_not_cached(monkeypatch, tmp_path):
    """Транзиентный сбой маршрутизации не закрепляется в кэше стадии:
    перезапуск после устранения причины собирает досье заново."""

    def boom(wd, p, targets, all_accounts):
        raise RuntimeError("budget exhausted")

    monkeypatch.setattr(dossier, "route_doc", boom)
    monkeypatch.setattr(dossier, "full_text", lambda wd, p: "text")
    monkeypatch.setattr(dossier, "doc_hash", lambda p: f"hash-{p.name}")
    d = dossier.build_dossiers(tmp_path, [Path("a.pdf")], INDEX)["ACC-1"]
    assert d["docs"] == []
    assert any(a["kind"] == "routing_failed" for a in d["alarms"])
    assert not (tmp_path / "dossier" / "ACC-1.json").exists()

    # «Причина устранена»: маршрутизация снова работает — досье пересобралось.
    make_route(monkeypatch, {"a.pdf": base("a.pdf")}, {"a.pdf": "text"})
    d2 = dossier.build_dossiers(tmp_path, [Path("a.pdf")], INDEX)["ACC-1"]
    assert [x["file"] for x in d2["docs"]] == ["a.pdf"]
    assert (tmp_path / "dossier" / "ACC-1.json").exists()


def test_build_failed_dossier_not_cached(monkeypatch, tmp_path):
    """Пустое досье из ветки dossier_build_failed не пишется артефактом:
    сбой чтения текста не переживает перезапуск."""
    routes = {"a.pdf": base("a.pdf")}
    make_route(monkeypatch, routes, {})

    def broken_text(wd, p):
        raise OSError("vision failed")

    monkeypatch.setattr(dossier, "full_text", broken_text)
    d = dossier.build_dossiers(tmp_path, [Path("a.pdf")], INDEX)["ACC-1"]
    assert d["docs"] == []
    assert d["alarms"][0]["kind"] == "dossier_build_failed"
    assert not (tmp_path / "dossier" / "ACC-1.json").exists()

    monkeypatch.setattr(dossier, "full_text", lambda wd, p: "text")
    d2 = dossier.build_dossiers(tmp_path, [Path("a.pdf")], INDEX)["ACC-1"]
    assert [x["file"] for x in d2["docs"]] == ["a.pdf"]
