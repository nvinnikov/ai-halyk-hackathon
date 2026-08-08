"""Кандидаты — только целевые счета; сшивка при нескольких кандидатах запрещена.

Фоновый документ (только нецелевые счета леджера) — штатный карантин без
алярма и без LLM-вызовов: на приватном наборе таких большинство.
"""

from pathlib import Path

import pytest

import route

TARGETS = ["ACC-1111", "ACC-2222"]
ALL_ACCOUNTS = ["ACC-1111", "ACC-2222", "ACC-9001"]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    state = {"text": "", "llm": []}

    monkeypatch.setattr(route, "full_text", lambda wd, p: state["text"])
    monkeypatch.setattr(route, "first_page_text", lambda wd, p: state["text"])
    monkeypatch.setattr(route, "doc_hash", lambda p: "cafe00000000")

    def fake_call(prompt, schema, schema_version, **kw):
        state["llm"].append(prompt)
        if schema is route.WHOSE_SCHEMA:
            return {"account_id": "ACC-1111", "quote": "договор с заёмщиком ACC-1111"}
        return {"doc_type": "agreement", "date": "2025-03-01", "edition": "final"}

    monkeypatch.setattr(route.llm, "call", fake_call)
    return state, tmp_path


def test_single_candidate_binds(fake):
    state, wd = fake
    state["text"] = "Договор займа, счёт заёмщика ACC-1111, фоновый счёт ACC-9001"
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] == "ACC-1111"
    assert art["quarantined"] is False
    assert art["alarms"] == []
    assert art["doc_type"] == "agreement" and art["edition"] == "final"
    assert art["mentions_nontarget"] == ["ACC-9001"]


def test_background_document_quarantined_without_alarm(fake):
    # Документ про чужой счёт отбрасывается штатно: без алярма и без LLM.
    state, wd = fake
    state["text"] = "упомянут только фоновый ACC-9001 и несуществующий ACC-0000"
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] is None and art["quarantined"] is True
    assert art["quarantine_reason"] == "background_document"
    assert art["alarms"] == []
    assert art["doc_type"] == "unrouted"
    assert state["llm"] == []  # ни META, ни WHOSE не вызывались


def test_no_accounts_at_all_is_alarm(fake):
    state, wd = fake
    state["text"] = "текст вовсе без номеров счетов"
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] is None and art["quarantined"] is True
    assert any(a["kind"] == "routing_quarantine" for a in art["alarms"])
    assert art["doc_type"] == "unrouted"
    assert state["llm"] == []


def test_multiple_candidates_go_to_llm_with_alarm(fake):
    state, wd = fake
    # Цитата WHOSE обязана находиться в тексте, иначе кандидат не подтверждён.
    state["text"] = "договор с заёмщиком ACC-1111, переводы между ACC-1111 и ACC-2222"
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] == "ACC-1111"
    assert art["routing_quote"] == "договор с заёмщиком ACC-1111"
    assert any(a["kind"] == "ambiguous_routing" for a in art["alarms"])


def test_llm_answer_outside_candidates_is_quarantine(fake, monkeypatch):
    state, wd = fake
    state["text"] = "переводы между ACC-1111 и ACC-2222"

    def bad_call(prompt, schema, schema_version, **kw):
        if schema is route.WHOSE_SCHEMA:
            return {"account_id": "ACC-9999", "quote": "..."}
        return {"doc_type": "other", "date": "", "edition": "unmarked"}

    monkeypatch.setattr(route.llm, "call", bad_call)
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] is None and art["quarantined"] is True


def test_unverified_whose_quote_quarantines(fake):
    state, wd = fake
    state["text"] = "переводы между ACC-1111 и ACC-2222"  # цитаты WHOSE в тексте нет
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] is None and art["quarantined"] is True
    assert any(a["kind"] == "quote_unverified" for a in art["alarms"])


def test_mention_search_respects_boundaries(fake):
    state, wd = fake
    # ACC-111 не должен находиться внутри ACC-1111 подстрочным поиском.
    state["text"] = "счёт заёмщика ACC-1111"
    art = route.route_doc(wd, Path("x.pdf"), ["ACC-111", "ACC-1111"], ["ACC-111", "ACC-1111"])
    assert art["mentions"] == ["ACC-1111"]
    assert art["account_id"] == "ACC-1111"


def test_non_client_doc_type_not_bound(fake, monkeypatch):
    """Методичка с целевым счётом внутри — не документ клиента: тип other
    не привязывается даже при единственном кандидате."""
    state, wd = fake

    def call_other(prompt, schema, schema_version, **kw):
        state["llm"].append(prompt)
        return {"doc_type": "other", "date": "", "edition": "unmarked"}

    monkeypatch.setattr(route.llm, "call", call_other)
    state["text"] = "методичка комплаенса, упоминает ACC-1111"
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] is None and art["quarantined"] is True
    assert art["quarantine_reason"] == "non_client_doc_type"
    assert not any(a["kind"] == "routing_quarantine" for a in art["alarms"])


NAMES = [("ACC-1111", "Alpha Terminal JSC"), ("ACC-2222", "Alpha Terminal Services JSC")]


def _group_meta_and_issuer(issuer):
    """META отвечает «отчётность», ISSUER называет издателя."""

    def call(prompt, schema, schema_version, **kw):
        if schema is route.ISSUER_SCHEMA:
            return {"reporting_entity": issuer, "quote": f"{issuer} consolidated statements"}
        return {"doc_type": "financial_notes", "date": "2025-12-31", "edition": "final"}

    return call


def test_name_match_respects_boundaries():
    assert route._name_mentioned("Alpha Terminal JSC", "сегмент ведёт Alpha Terminal JSC, а также")
    # Наименования заёмщиков в наборе различаются одним словом: документ соседа
    # не должен подтягиваться к чужому счёту.
    assert not route._name_mentioned("Alpha Terminal JSC", "сегмент ведёт Alpha Terminal Services JSC")
    # Границы обязательны и справа: наименование не бывает куском слова.
    assert not route._name_mentioned("Alpha Terminal JSC", "Alpha Terminal JSCX")


def test_group_level_doc_attached_by_name(fake, monkeypatch):
    state, wd = fake
    state["text"] = "Parent Holding JSC consolidated statements. Сегмент ведёт Alpha Terminal JSC."

    monkeypatch.setattr(route.llm, "call", _group_meta_and_issuer("Parent Holding JSC"))
    art = route.route_group_doc(wd, Path("x.pdf"), NAMES)
    assert art["account_id"] == "ACC-1111"
    assert art["quarantined"] is False
    assert any(a["kind"] == "group_doc_attached" for a in art["alarms"])


def test_named_doc_of_other_type_not_attached(fake, monkeypatch):
    """Внутренний регламент печатает наименование заёмщика в шапке и по имени
    находится; от досье его отделяет только тип документа."""
    state, wd = fake
    state["text"] = "Alpha Terminal JSC — операционное руководство подразделения"

    def meta_other(prompt, schema, schema_version, **kw):
        return {"doc_type": "other", "date": "", "edition": "unmarked"}

    monkeypatch.setattr(route.llm, "call", meta_other)
    art = route.route_group_doc(wd, Path("x.pdf"), NAMES)
    assert art["account_id"] is None and art["quarantined"] is True
    assert art["quarantine_reason"] == "named_doc_not_group_level"
    assert art["alarms"] == []  # документ уже в карантине первого прохода


def test_two_named_borrowers_are_not_stitched(fake):
    state, wd = fake
    state["text"] = "Alpha Terminal JSC и Alpha Terminal Services JSC — обе в периметре"
    art = route.route_group_doc(wd, Path("x.pdf"), NAMES)
    assert art["account_id"] is None
    assert art["quarantine_reason"] == "ambiguous_named_borrowers"
    assert state["llm"] == []  # META при неоднозначности не зовётся


def test_borrower_name_must_be_verbatim(fake, monkeypatch):
    """Наименованием ищут заёмщика в чужом документе обычным поиском: форма,
    которой в тексте нет, не годится, даже если по смыслу верна."""
    state, wd = fake
    state["text"] = "Заёмщик — Alpha Terminal JSC, счёт ACC-1111"

    def paraphrased(prompt, schema, schema_version, **kw):
        return {"name": "АО «Альфа Терминал»", "quote": "Заёмщик — Alpha Terminal JSC"}

    monkeypatch.setattr(route.llm, "call", paraphrased)
    art = route.borrower_name(wd, "ACC-1111", [Path("x.pdf")])
    assert art["name"] == ""
    assert any(a["kind"] == "quote_unverified" for a in art["alarms"])
    assert not (wd / "borrower" / "ACC-1111.json").exists()  # деградация не кэшируется


def test_meta_failure_not_cached(fake, monkeypatch):
    """Провал META (SchemaRejected → карантин non_client_doc_type) не
    оставляет route-артефакта: перезапуск после устранения причины
    перемаршрутизирует (ревью PR #9, 23-я волна)."""
    import llm as llm_mod

    state, wd = fake

    def meta_fails(prompt, schema, schema_version, **kw):
        raise llm_mod.SchemaRejected("schema mismatch")

    monkeypatch.setattr(route.llm, "call", meta_fails)
    state["text"] = "Договор займа, счёт заёмщика ACC-1111"
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert any(a["kind"] == "meta_extraction_failed" for a in art["alarms"])
    assert not (wd / "route" / "cafe00000000.json").exists()

    # Причина устранена — маршрутизация проходит и кэшируется.
    def meta_ok(prompt, schema, schema_version, **kw):
        return {"doc_type": "agreement", "date": "2025-03-01", "edition": "final"}

    monkeypatch.setattr(route.llm, "call", meta_ok)
    art2 = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art2["account_id"] == "ACC-1111"
    assert (wd / "route" / "cafe00000000.json").exists()


def test_own_reporting_without_account_not_attached(fake, monkeypatch):
    """Без проверки издателя правило читается как «называет заёмщика и не
    печатает номер счёта» — под это подходит и собственная отчётность заёмщика,
    а она в роли группового документа теряет свои реклассификации и отдаёт свои
    основные средства за капзатраты Группы (ревью PR #23, вторая волна)."""
    state, wd = fake
    state["text"] = "Alpha Terminal JSC consolidated statements. Отчётность за год."
    monkeypatch.setattr(route.llm, "call", _group_meta_and_issuer("Alpha Terminal JSC"))
    art = route.route_group_doc(wd, Path("x.pdf"), NAMES)
    assert art["account_id"] is None and art["quarantined"] is True
    assert art["quarantine_reason"] == "own_reporting_not_group_level"
    assert any(a["kind"] == "own_reporting_rejected" for a in art["alarms"])


def test_name_search_survives_line_break(fake):
    """verify_quote, которым наименование допущено, пробелы схлопывает — поиск
    обязан вести себя так же, иначе перенос строки в вёрстке PDF даёт
    молчаливый промах на единственной ячейке, ради которой проход и делается."""
    assert route._name_mentioned("Alpha Terminal JSC", "ведёт Alpha Terminal\nServices") is False
    assert route._name_mentioned("Alpha Terminal JSC", "ведёт Alpha Terminal\nJSC, сегмент")
    assert route._name_mentioned("Alpha Terminal JSC", "ведёт Alpha  Terminal JSC")


def test_generic_borrower_name_rejected(fake, monkeypatch):
    """Наименованием ищут заёмщика в произвольных чужих документах: общее слово
    прошло бы verify_quote по его собственным страницам и совпало бы везде."""
    state, wd = fake
    state["text"] = "Заёмщик — Группа, счёт ACC-1111"

    def generic(prompt, schema, schema_version, **kw):
        return {"name": "Группа", "quote": "Заёмщик — Группа"}

    monkeypatch.setattr(route.llm, "call", generic)
    art = route.borrower_name(wd, "ACC-1111", [Path("x.pdf")])
    assert art["name"] == ""
    assert any(a["kind"] == "borrower_name_too_generic" for a in art["alarms"])
