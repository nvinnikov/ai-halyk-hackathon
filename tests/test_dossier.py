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
