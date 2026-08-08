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


def test_cumulative_type_drops_superseded_edition(monkeypatch, tmp_path):
    """Кумулятивность — про то, что редакции не выбирают по дате, а не про
    право читать замененный черновик. Рабочий документ с маркером «заменён»
    несёт предварительное решение; применить его — исказить ковенант."""
    routes = {
        "final.pdf": base("final.pdf", dtype="audit_report", date="", edition="final"),
        "draft.pdf": base("draft.pdf", dtype="audit_report", date="", edition="superseded"),
    }
    make_route(monkeypatch, routes, {})
    d = dossier.build_dossiers(tmp_path, [Path("final.pdf"), Path("draft.pdf")], INDEX)["ACC-1"]
    assert [x["file"] for x in d["docs"]] == ["final.pdf"]
    assert d["docs_rejected"] == [{"file": "draft.pdf", "reason": "superseded_edition", "kept": "final.pdf"}]


def test_cumulative_type_drops_lone_draft(monkeypatch, tmp_path):
    """Черновик кумулятивного типа не несёт решений — даже единственный.

    Промежуточная ведомость аудитора сама пишет, что она рабочий документ, не
    окончательная позиция, и что первоначальная классификация сохраняется.
    Отбросить её — значит оставить операцию в исходной категории, то есть ровно
    то, что документ и предписывает. Граница «draft против superseded» здесь
    решается кодом, а не моделью: в тексте таких ведомостей стоят оба маркера
    сразу, и выбор между ними был бы недетерминирован."""
    routes = {"d.pdf": base("d.pdf", dtype="audit_report", date="", edition="draft")}
    make_route(monkeypatch, routes, {})
    d = dossier.build_dossiers(tmp_path, [Path("d.pdf")], INDEX)["ACC-1"]
    assert d["docs"] == []
    assert d["docs_rejected"] == [{"file": "d.pdf", "reason": "draft_edition", "kept": None}]


def test_cumulative_type_keeps_draft_treasury_memo(monkeypatch, tmp_path):
    """Записка казначейства — рабочий документ по природе: окончательной формы у
    неё нет, заменённой она себя не объявляет, и её черновик несёт настоящее
    исправление суммы. Правило про черновики касается только типов с
    окончательной формой."""
    routes = {"m.pdf": base("m.pdf", dtype="treasury_memo", date="", edition="draft")}
    make_route(monkeypatch, routes, {})
    d = dossier.build_dossiers(tmp_path, [Path("m.pdf")], INDEX)["ACC-1"]
    assert [x["file"] for x in d["docs"]] == ["m.pdf"]


def test_cumulative_type_drops_superseded_treasury_memo(monkeypatch, tmp_path):
    """Маркер «заменён» отменяет документ любого кумулятивного типа: тут
    сомнений в редакции нет, в отличие от черновика."""
    routes = {"m.pdf": base("m.pdf", dtype="treasury_memo", date="", edition="superseded")}
    make_route(monkeypatch, routes, {})
    d = dossier.build_dossiers(tmp_path, [Path("m.pdf")], INDEX)["ACC-1"]
    assert d["docs"] == []


def test_noncumulative_type_keeps_lone_draft(monkeypatch, tmp_path):
    """Договор — не решение, а источник самого ковенанта: единственный черновик
    договора остаётся, иначе считать нечего. Правило про черновики касается
    только типов, несущих документальные решения."""
    routes = {"a.pdf": base("a.pdf", dtype="agreement", edition="draft")}
    make_route(monkeypatch, routes, {})
    d = dossier.build_dossiers(tmp_path, [Path("a.pdf")], INDEX)["ACC-1"]
    assert [x["file"] for x in d["docs"]] == ["a.pdf"]


def test_cumulative_type_all_superseded_leaves_no_docs(monkeypatch, tmp_path):
    """Ни одной действующей редакции — досье без документов этого типа, а не
    откат к замененному: тот же инвариант, что у некумулятивных типов."""
    routes = {"a.pdf": base("a.pdf", dtype="audit_report", date="", edition="superseded")}
    make_route(monkeypatch, routes, {})
    d = dossier.build_dossiers(tmp_path, [Path("a.pdf")], INDEX)["ACC-1"]
    assert d["docs"] == []
    assert d["docs_rejected"] == [{"file": "a.pdf", "reason": "superseded_edition", "kept": None}]


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


def test_degraded_run_reads_cached_good_dossier(monkeypatch, tmp_path):
    """Перезапуск с транзиентным сбоем маршрутизации не хуже сохранённого
    состояния: хорошее досье с прошлого прогона читается из кэша, а не
    заменяется свежесобранным неполным (ревью PR #9, 23-я волна)."""
    make_route(monkeypatch, {"a.pdf": base("a.pdf")}, {"a.pdf": "good text"})
    d1 = dossier.build_dossiers(tmp_path, [Path("a.pdf")], INDEX)["ACC-1"]
    assert [x["file"] for x in d1["docs"]] == ["a.pdf"]

    def boom(wd, p, targets, all_accounts):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(dossier, "route_doc", boom)
    d2 = dossier.build_dossiers(tmp_path, [Path("a.pdf")], INDEX)["ACC-1"]
    assert [x["file"] for x in d2["docs"]] == ["a.pdf"]  # кэш, не пустое досье


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


def test_group_doc_attached_by_name_gets_own_scope(monkeypatch, tmp_path):
    """Документ группового уровня приходит в досье отдельной областью видимости
    и НЕ участвует в выборе действующей редакции по своему типу: он про
    материнскую компанию, а не про заёмщика."""
    routes = {
        "notes.pdf": base("notes.pdf", dtype="financial_notes", date="2025-06-01"),
        "group.pdf": base("group.pdf", acc=None, reason="no_account_mentions"),
    }
    make_route(monkeypatch, routes, {"notes.pdf": "своё", "group.pdf": "консолидация"})
    monkeypatch.setattr(dossier, "borrower_name", lambda wd, acc, paths: {"name": "Alpha JSC", "alarms": []})
    monkeypatch.setattr(
        dossier,
        "route_group_doc",
        lambda wd, p, names: {
            **base(p.name, dtype="financial_notes", date="2025-12-31"),
            "alarms": [{"kind": "group_doc_attached", "file": p.name, "account": "ACC-1"}],
        },
    )
    d = dossier.build_dossiers(tmp_path, [Path("notes.pdf"), Path("group.pdf")], INDEX)["ACC-1"]
    assert [(x["file"], x["scope"]) for x in d["docs"]] == [
        ("notes.pdf", "borrower"),
        ("group.pdf", "group"),
    ]
    assert d["docs_rejected"] == []
    assert any(a["kind"] == "group_doc_attached" for a in d["alarms"])


def test_background_document_not_offered_to_name_pass(monkeypatch, tmp_path):
    """Второй проход берёт только документы без счетов вовсе: там, где счёт
    напечатан, решение уже принято и наименованием не переигрывается."""
    routes = {"bg.pdf": base("bg.pdf", acc=None, reason="background_document")}
    make_route(monkeypatch, routes, {})

    def boom(*a, **kw):
        raise AssertionError("второй проход не должен трогать фоновый документ")

    monkeypatch.setattr(dossier, "borrower_name", boom)
    monkeypatch.setattr(dossier, "route_group_doc", boom)
    d = dossier.build_dossiers(tmp_path, [Path("bg.pdf")], INDEX)["ACC-1"]
    assert d["docs"] == []


def test_name_pass_skipped_when_name_pool_degraded(monkeypatch, tmp_path):
    """Артефакт route_group кэшируется по хешу документа, и пул наименований в
    ключ не входит: отказ, посчитанный при неполном пуле, пережил бы устранение
    причины. При транзиентном сбое имени проход не делается вовсе."""
    routes = {
        "a.pdf": base("a.pdf"),
        "group.pdf": base("group.pdf", acc=None, reason="no_account_mentions"),
    }
    make_route(monkeypatch, routes, {})

    def boom(wd, acc, paths):
        raise RuntimeError("бюджет исчерпан")

    monkeypatch.setattr(dossier, "borrower_name", boom)

    def never(*a, **kw):
        raise AssertionError("привязка по имени при неполном пуле запрещена")

    monkeypatch.setattr(dossier, "route_group_doc", never)
    d = dossier.build_dossiers(tmp_path, [Path("a.pdf"), Path("group.pdf")], INDEX)["ACC-1"]
    assert any(a["kind"] == "borrower_name_failed" for a in d["alarms"])
    assert not (tmp_path / "dossier" / "ACC-1.json").exists()  # деградация не кэшируется


def test_name_pass_skipped_when_first_pass_degraded(monkeypatch, tmp_path):
    """Второй проход целиком построен на результатах первого. При его
    деградации и наименование, и пул неполны, а кэшируются как успех: ни набор
    документов, ни пул в ключи артефактов не входят (ревью PR #23)."""
    routes = {
        "a.pdf": base("a.pdf"),
        "broken.pdf": {
            **base("broken.pdf", acc=None, reason="routing_failed"),
            "alarms": [{"kind": "routing_failed", "file": "broken.pdf", "error": "boom"}],
        },
        "group.pdf": base("group.pdf", acc=None, reason="no_account_mentions"),
    }
    make_route(monkeypatch, routes, {})

    def never(*a, **kw):
        raise AssertionError("второй проход при деградации первого запрещён")

    monkeypatch.setattr(dossier, "borrower_name", never)
    monkeypatch.setattr(dossier, "route_group_doc", never)
    pdfs = [Path("a.pdf"), Path("broken.pdf"), Path("group.pdf")]
    d = dossier.build_dossiers(tmp_path, pdfs, INDEX)["ACC-1"]
    assert any(a["kind"] == "name_pass_skipped_degraded_routing" for a in d["alarms"])
    assert not (tmp_path / "dossier" / "ACC-1.json").exists()  # деградация не кэшируется


def test_group_meta_failure_does_not_block_dossier_cache(monkeypatch, tmp_path):
    """Второй проход зовёт META по каждому непривязанному документу (на публичном
    наборе их 122). Под общим именем один SchemaRejected среди них ставил бы
    degraded=True и запрещал запись ВСЕХ досье — то есть отменял бы рестарт в
    окне из-за документа, который и так остаётся в карантине (ревью PR #23)."""
    routes = {
        "a.pdf": base("a.pdf"),
        "junk.pdf": base("junk.pdf", acc=None, reason="no_account_mentions"),
    }
    make_route(monkeypatch, routes, {})
    monkeypatch.setattr(dossier, "borrower_name", lambda wd, acc, paths: {"name": "Alpha JSC", "alarms": []})
    monkeypatch.setattr(
        dossier,
        "route_group_doc",
        lambda wd, p, names: {
            **base(p.name, acc=None, reason="named_doc_not_group_level"),
            "alarms": [{"kind": "meta_extraction_failed", "file": p.name}],
        },
    )
    d = dossier.build_dossiers(tmp_path, [Path("a.pdf"), Path("junk.pdf")], INDEX)["ACC-1"]
    assert any(a["kind"] == "group_meta_extraction_failed" for a in d["alarms"])
    assert (tmp_path / "dossier" / "ACC-1.json").exists()  # рестарт не отменён


def test_warm_dossiers_skip_the_name_pass(monkeypatch, tmp_path):
    """Все досье уже текущей версии — artifact() вернёт их готовыми, а проход
    успел бы прочитать полный текст сотни документов впустую. В окне это прямая
    цена рестарта (ревью PR #23, вторая волна)."""
    routes = {
        "a.pdf": base("a.pdf"),
        "group.pdf": base("group.pdf", acc=None, reason="no_account_mentions"),
    }
    make_route(monkeypatch, routes, {})
    monkeypatch.setattr(dossier, "borrower_name", lambda wd, acc, paths: {"name": "Alpha JSC", "alarms": []})
    monkeypatch.setattr(
        dossier,
        "route_group_doc",
        lambda wd, p, names: base(p.name, acc=None, reason="named_doc_not_group_level"),
    )
    pdfs = [Path("a.pdf"), Path("group.pdf")]
    dossier.build_dossiers(tmp_path, pdfs, INDEX)  # прогрев

    def never(*a, **kw):
        raise AssertionError("тёплые досье не должны запускать второй проход")

    monkeypatch.setattr(dossier, "borrower_name", never)
    monkeypatch.setattr(dossier, "route_group_doc", never)
    d = dossier.build_dossiers(tmp_path, pdfs, INDEX)["ACC-1"]
    assert [x["file"] for x in d["docs"]] == ["a.pdf"]
